"""Tests for Smoketree's parsing, caching, and backend logic."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from smoketree import cache as cachelib
from smoketree.backends.base import ExecutionContext
from smoketree.backends.claude import ClaudeBackend
from smoketree.backends.comfyui import ComfyUIBackend
from smoketree.backends.ollama import OllamaBackend
from smoketree.cache import Artifact
from smoketree.errors import SmoketreeError, ValidationError
from smoketree.graph import load_graph
from smoketree.loader import substitute_env
from smoketree.models import (
    ClaudeTransformer,
    ComfyCollect,
    ComfyInject,
    ComfyUITransformer,
    InputSpec,
    OllamaTransformer,
    OutputSpec,
)
from smoketree.project import Project
from smoketree.scaffold import init_project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    # The "demo" template provides the `shout` transformer + textproc script that
    # most graph/collection tests build on, plus the demo graph itself.
    init_project(tmp_path, "test-proj", template="demo")
    return Project(tmp_path)


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


def test_scaffold_writes_instructions(project: Project):
    instructions = project.root / "INSTRUCTIONS.md"
    assert instructions.exists()
    assert "Smoketree project" in instructions.read_text()


# --------------------------------------------------------------------------- #
# init templates
# --------------------------------------------------------------------------- #


def test_init_default_is_minimal(tmp_path: Path):
    init_project(tmp_path, "p")  # default template
    assert (tmp_path / "smoketree.yaml").exists()
    assert (tmp_path / "INSTRUCTIONS.md").exists()
    # standard dirs exist but no example graphs
    assert (tmp_path / "graphs").is_dir()
    assert (tmp_path / "transformers").is_dir()
    assert not list((tmp_path / "graphs").glob("*.yaml"))


def test_init_substitutes_project_name(tmp_path: Path):
    init_project(tmp_path, "my-cool-proj")
    config = (tmp_path / "smoketree.yaml").read_text()
    assert "name: my-cool-proj" in config
    assert "__PROJECT_NAME__" not in config


def test_init_template_demo(tmp_path: Path):
    init_project(tmp_path, "p", template="demo")
    assert (tmp_path / "graphs" / "demo.yaml").exists()
    assert (tmp_path / "transformers" / "shout.yaml").exists()
    assert (tmp_path / "scripts" / "textproc.py").exists()


def test_init_template_portrait(tmp_path: Path):
    init_project(tmp_path, "p", template="portrait")
    assert (tmp_path / "graphs" / "portrait.yaml").exists()
    txt2img = (tmp_path / "transformers" / "txt2img.yaml").read_text()
    assert "seed_inject" in txt2img  # demonstrates the seed injection feature


def test_init_unknown_template_errors(tmp_path: Path):
    with pytest.raises(SmoketreeError, match="Unknown template"):
        init_project(tmp_path, "p", template="nope")


def test_list_templates():
    from smoketree.scaffold import list_templates

    templates = list_templates()
    assert "minimal" in templates
    assert "portrait" in templates
    assert all(isinstance(v, str) for v in templates.values())


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert substitute_env({"k": "${FOO}/x"}) == {"k": "bar/x"}
    assert substitute_env(["${FOO}", 1]) == ["bar", 1]


def test_env_substitution_missing(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(SmoketreeError):
        substitute_env("${NOPE}")


# --------------------------------------------------------------------------- #
# Graph parsing / validation
# --------------------------------------------------------------------------- #


def test_demo_topological_order(project: Project):
    graph = load_graph(project, "demo")
    assert graph.execution_order == ["text", "shout", "reverse", "stats"]


def test_portrait_topological_order(tmp_path: Path):
    init_project(tmp_path, "p", template="portrait")
    graph = load_graph(Project(tmp_path), "portrait")
    assert graph.execution_order == [
        "source",
        "description",
        "prompt",
        "generated",
        "upscaled",
    ]


def test_cycle_detection(project: Project):
    (project.graphs_dir / "cyc.yaml").write_text(
        "name: cyc\n"
        "nodes:\n"
        "  a: {type: transform, transformer: shout, inputs: {input: b}}\n"
        "  b: {type: transform, transformer: shout, inputs: {input: a}}\n"
    )
    with pytest.raises(ValidationError, match="cycle"):
        load_graph(project, "cyc")


def test_media_type_mismatch(project: Project):
    (project.transformers_dir / "needs_image.yaml").write_text(
        "name: needs_image\n"
        "type: shell\n"
        "command: cp {inputs.image} {outputs.x}\n"
        "inputs:\n  image: {type: file, media: image}\n"
        "outputs:\n  x: {type: file, media: data}\n"
    )
    (project.graphs_dir / "mm.yaml").write_text(
        "name: mm\n"
        "nodes:\n"
        "  t: {type: source, path: sources/hello.txt}\n"  # text feeding an image input
        "  b: {type: transform, transformer: needs_image, inputs: {image: t}}\n"
    )
    with pytest.raises(ValidationError, match="Media type mismatch"):
        load_graph(project, "mm")


def test_unknown_input_rejected(project: Project):
    (project.graphs_dir / "ui.yaml").write_text(
        "name: ui\n"
        "nodes:\n"
        "  t: {type: source, path: sources/hello.txt}\n"
        "  s: {type: transform, transformer: shout, inputs: {input: t, bogus: t}}\n"
    )
    with pytest.raises(ValidationError, match="unknown inputs"):
        load_graph(project, "ui")


def test_subgraph_order(project: Project):
    graph = load_graph(project, "demo")
    assert graph.subgraph_order("reverse") == ["text", "shout", "reverse"]


# --------------------------------------------------------------------------- #
# Cache / seed
# --------------------------------------------------------------------------- #


def test_seed_deterministic():
    a = cachelib.compute_seed("g", "n", 0)
    b = cachelib.compute_seed("g", "n", 0)
    c = cachelib.compute_seed("g", "n", 1)
    assert a == b
    assert a != c
    assert 0 <= a < 2**32


def test_cache_key_changes_with_input():
    base = cachelib.compute_cache_key({"x": "h1"}, "tf", None, 0)
    changed_input = cachelib.compute_cache_key({"x": "h2"}, "tf", None, 0)
    changed_tf = cachelib.compute_cache_key({"x": "h1"}, "tf2", None, 0)
    changed_take = cachelib.compute_cache_key({"x": "h1"}, "tf", None, 1)
    assert len({base, changed_input, changed_tf, changed_take}) == 4


def test_cache_key_stable_across_input_order():
    a = cachelib.compute_cache_key({"x": "1", "y": "2"}, "tf", None, 0)
    b = cachelib.compute_cache_key({"y": "2", "x": "1"}, "tf", None, 0)
    assert a == b


# --------------------------------------------------------------------------- #
# Claude backend (prompt assembly, no network)
# --------------------------------------------------------------------------- #


def _ctx(tmp_path, transformer, inputs) -> ExecutionContext:
    return ExecutionContext(
        project=None,  # type: ignore[arg-type]
        graph_id="g",
        node_id="n",
        transformer=transformer,
        inputs=inputs,
        output_targets={"out": tmp_path / "out.txt"},
        scratch_dir=tmp_path,
        output_dir=tmp_path,
        seed=1,
        take=0,
    )


def test_claude_inlines_text(tmp_path):
    text_file = tmp_path / "in.txt"
    text_file.write_text("HELLO")
    tf = ClaudeTransformer(
        name="x",
        type="claude",
        prompt="Say: {inputs.text}",
        inputs={"text": InputSpec(media="text")},
        outputs={"out": OutputSpec(media="text", format="txt")},
    )
    art = Artifact(path=text_file, media="text", format="txt", content_hash="h")
    prompt, images = ClaudeBackend()._build_prompt(_ctx(tmp_path, tf, {"text": art}))
    assert prompt == "Say: HELLO"
    assert images == []


def test_claude_attaches_image(tmp_path):
    img = tmp_path / "in.png"
    img.write_bytes(b"\x89PNG\r\n")
    tf = ClaudeTransformer(
        name="x",
        type="claude",
        prompt="Describe: {inputs.image}",
        inputs={"image": InputSpec(media="image")},
        outputs={"out": OutputSpec(media="text", format="txt")},
    )
    art = Artifact(path=img, media="image", format="png", content_hash="h")
    prompt, images = ClaudeBackend()._build_prompt(_ctx(tmp_path, tf, {"image": art}))
    assert "Describe:" in prompt
    assert len(images) == 1
    assert images[0]["type"] == "image"
    assert images[0]["source"]["media_type"] == "image/png"


# --------------------------------------------------------------------------- #
# Ollama backend (payload assembly, no network)
# --------------------------------------------------------------------------- #


def _ollama_tf(prompt, inputs, **kw) -> OllamaTransformer:
    return OllamaTransformer(
        name="x",
        type="ollama",
        model=kw.pop("model", "llama3.2"),
        prompt=prompt,
        inputs=inputs,
        outputs={"out": OutputSpec(media="text", format="txt")},
        **kw,
    )


def test_ollama_payload_text(tmp_path):
    text_file = tmp_path / "in.txt"
    text_file.write_text("a sunset")
    tf = _ollama_tf("Rewrite: {inputs.text}", {"text": InputSpec(media="text")},
                    system="be concise")
    art = Artifact(path=text_file, media="text", format="txt", content_hash="h")
    payload = OllamaBackend()._build_payload(_ctx(tmp_path, tf, {"text": art}))
    assert payload["prompt"] == "Rewrite: a sunset"
    assert payload["model"] == "llama3.2"
    assert payload["system"] == "be concise"
    assert payload["stream"] is False
    assert payload["options"]["seed"] == 1  # injected from ctx.seed
    assert "images" not in payload


def test_ollama_payload_image(tmp_path):
    img = tmp_path / "in.png"
    img.write_bytes(b"\x89PNG\r\n")
    tf = _ollama_tf("Describe: {inputs.image}", {"image": InputSpec(media="image")},
                    model="llava")
    art = Artifact(path=img, media="image", format="png", content_hash="h")
    payload = OllamaBackend()._build_payload(_ctx(tmp_path, tf, {"image": art}))
    assert payload["model"] == "llava"
    assert len(payload["images"]) == 1
    assert isinstance(payload["images"][0], str)  # base64


def test_ollama_empty_response_fails(tmp_path, monkeypatch):
    text_file = tmp_path / "in.txt"
    text_file.write_text("x")
    tf = _ollama_tf("{inputs.text}", {"text": InputSpec(media="text")})
    art = Artifact(path=text_file, media="text", format="txt", content_hash="h")

    class _Proj:
        class config:
            class defaults:
                ollama_url = "http://localhost:11434"

    ctx = ExecutionContext(
        project=_Proj(),  # type: ignore[arg-type]
        graph_id="g", node_id="n", transformer=tf, inputs={"text": art},
        output_targets={"out": tmp_path / "out.txt"},
        scratch_dir=tmp_path, output_dir=tmp_path, seed=1, take=0,
    )

    class _EmptyResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "   \n", "done_reason": "stop", "eval_count": 60}

    class _EmptyClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            return _EmptyResp()

    monkeypatch.setattr("smoketree.backends.ollama.httpx.Client",
                        lambda *a, **k: _EmptyClient())
    with pytest.raises(SmoketreeError, match="empty response"):
        OllamaBackend().execute(ctx)
    assert not (tmp_path / "out.txt").exists()  # no output written on failure


def test_ollama_think_param(tmp_path):
    text_file = tmp_path / "in.txt"
    text_file.write_text("x")
    art = Artifact(path=text_file, media="text", format="txt", content_hash="h")

    # omitted by default
    tf = _ollama_tf("{inputs.text}", {"text": InputSpec(media="text")})
    payload = OllamaBackend()._build_payload(_ctx(tmp_path, tf, {"text": art}))
    assert "think" not in payload

    # passed through when set
    tf = _ollama_tf("{inputs.text}", {"text": InputSpec(media="text")}, think=False)
    payload = OllamaBackend()._build_payload(_ctx(tmp_path, tf, {"text": art}))
    assert payload["think"] is False


def test_ollama_explicit_seed_preserved(tmp_path):
    text_file = tmp_path / "in.txt"
    text_file.write_text("x")
    tf = _ollama_tf("{inputs.text}", {"text": InputSpec(media="text")},
                    options={"seed": 999, "temperature": 0.5})
    art = Artifact(path=text_file, media="text", format="txt", content_hash="h")
    payload = OllamaBackend()._build_payload(_ctx(tmp_path, tf, {"text": art}))
    assert payload["options"]["seed"] == 999
    assert payload["options"]["temperature"] == 0.5


# --------------------------------------------------------------------------- #
# ComfyUI backend (injection + collection, faked client)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, content=b"", json_data=None):
        self.content = content
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self):
        self.downloaded = []

    def get(self, url, params=None):
        self.downloaded.append(params)
        return _FakeResponse(content=b"IMAGEBYTES")


def test_comfyui_inject_text(tmp_path):
    prompt_file = tmp_path / "p.txt"
    prompt_file.write_text("a cat")
    tf = ComfyUITransformer(
        name="x",
        type="comfyui",
        workflow="w.json",
        inputs={
            "prompt": InputSpec(media="text", inject=ComfyInject(node_id="6", field="text"))
        },
        outputs={
            "image": OutputSpec(
                media="image", format="png",
                collect=ComfyCollect(node_id="9", field="filename_prefix"),
            )
        },
    )
    art = Artifact(path=prompt_file, media="text", format="txt", content_hash="h")
    ctx = _ctx(tmp_path, tf, {"prompt": art})
    workflow = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}}}
    ComfyUIBackend()._inject_inputs(_FakeClient(), workflow, ctx)
    assert workflow["6"]["inputs"]["text"] == "a cat"


def test_comfyui_seed_injection(tmp_path):
    tf = ComfyUITransformer(
        name="x",
        type="comfyui",
        workflow="w.json",
        seed_inject=ComfyInject(node_id="3", field="seed"),
        inputs={},
        outputs={
            "image": OutputSpec(
                media="image", format="png",
                collect=ComfyCollect(node_id="9", field="filename_prefix"),
            )
        },
    )
    ctx = _ctx(tmp_path, tf, {})  # _ctx sets seed=1
    workflow = {"3": {"class_type": "KSampler", "inputs": {"seed": 0}}}
    ComfyUIBackend()._inject_inputs(_FakeClient(), workflow, ctx)
    assert workflow["3"]["inputs"]["seed"] == ctx.seed == 1


def test_comfyui_seed_injection_unknown_node(tmp_path):
    tf = ComfyUITransformer(
        name="x", type="comfyui", workflow="w.json",
        seed_inject=ComfyInject(node_id="99", field="seed"),
        inputs={},
        outputs={"image": OutputSpec(media="image", format="png",
            collect=ComfyCollect(node_id="9", field="filename_prefix"))},
    )
    ctx = _ctx(tmp_path, tf, {})
    with pytest.raises(SmoketreeError, match="seed_inject"):
        ComfyUIBackend()._inject_inputs(_FakeClient(), {"3": {"inputs": {}}}, ctx)


def test_comfyui_collect_outputs(tmp_path):
    tf = ComfyUITransformer(
        name="x",
        type="comfyui",
        workflow="w.json",
        inputs={},
        outputs={
            "image": OutputSpec(
                media="image", format="png",
                collect=ComfyCollect(node_id="9", field="filename_prefix"),
            )
        },
    )
    ctx = ExecutionContext(
        project=None,  # type: ignore[arg-type]
        graph_id="g", node_id="n", transformer=tf, inputs={},
        output_targets={"image": tmp_path / "image.png"},
        scratch_dir=tmp_path, output_dir=tmp_path, seed=1, take=0,
    )
    history = {
        "outputs": {
            "9": {"images": [{"filename": "out_001.png", "subfolder": "", "type": "output"}]}
        }
    }
    produced = ComfyUIBackend()._collect_outputs(_FakeClient(), history, ctx)
    assert produced["image"].read_bytes() == b"IMAGEBYTES"
    assert produced["image"].suffix == ".png"


# --------------------------------------------------------------------------- #
# End-to-end shell pipeline + caching
# --------------------------------------------------------------------------- #


def test_end_to_end_run_and_cache(project: Project, capsys):
    from smoketree.executor import compute_plan, run

    graph = load_graph(project, "demo")
    run(project, graph, take=0)

    # All outputs produced.
    stats = cachelib.cache_node_dir(project, "demo", "stats", 0) / "text.txt"
    assert stats.exists()

    # Second plan: everything cached.
    entries = {e.node_id: e.action for e in compute_plan(project, graph, take=0)}
    assert entries == {
        "text": "SKIP", "shout": "SKIP", "reverse": "SKIP", "stats": "SKIP",
    }

    # Modify source -> downstream invalidated.
    (project.sources_dir / "hello.txt").write_text("changed!\n")
    entries = {e.node_id: e.action for e in compute_plan(project, graph, take=0)}
    assert entries["shout"] == "RUN"
    assert entries["reverse"] == "RUN"


def test_output_symlink_created(project: Project):
    from smoketree.executor import run

    graph = load_graph(project, "demo")
    run(project, graph, take=0)
    link = project.outputs_dir / "demo__stats.txt"
    assert link.exists()


def test_take_changes_seed_and_dir(project: Project):
    from smoketree.executor import run

    graph = load_graph(project, "demo")
    run(project, graph, take=0)
    run(project, graph, take=2)
    assert cachelib.cache_node_dir(project, "demo", "shout", 2).exists()
    assert (cachelib.cache_node_dir(project, "demo", "shout", 0)
            != cachelib.cache_node_dir(project, "demo", "shout", 2))


# --------------------------------------------------------------------------- #
# Collections and fan-out
# --------------------------------------------------------------------------- #


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


_PAIR_TRANSFORMER = (
    "name: pair\n"
    "type: shell\n"
    "command: cat {inputs.a} {inputs.b} > {outputs.text}\n"
    "inputs:\n"
    "  a: {type: file, media: text}\n"
    "  b: {type: file, media: text}\n"
    "outputs:\n"
    "  text: {type: file, media: text, format: txt}\n"
)


@pytest.fixture
def collections_project(project: Project) -> Project:
    for n in ("1", "2"):
        _write(project.sources_dir / "a" / f"{n}.txt", f"a{n}\n")
    for n in ("x", "y", "z"):
        _write(project.sources_dir / "b" / f"{n}.txt", f"b{n}\n")
    (project.transformers_dir / "pair.yaml").write_text(_PAIR_TRANSFORMER)
    return project


def test_collection_is_flagged_and_propagates(project: Project):
    _write(project.sources_dir / "a" / "1.txt", "a1\n")
    (project.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  items: {type: collection, glob: sources/a/*.txt}\n"
        "  up: {type: transform, transformer: shout, inputs: {input: items}, "
        "expand: each}\n"
    )
    graph = load_graph(project, "g")
    assert graph.is_collection("items") is True
    assert graph.is_collection("up") is True  # consumes a collection


def test_each_fanout_runs_per_item(project: Project):
    from smoketree.executor import run
    from smoketree.cache import State

    for n in ("1", "2", "3"):
        _write(project.sources_dir / "a" / f"{n}.txt", f"a{n}\n")
    (project.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  items: {type: collection, glob: sources/a/*.txt}\n"
        "  up: {type: transform, transformer: shout, inputs: {input: items}, "
        "expand: each}\n"
    )
    graph = load_graph(project, "g")
    run(project, graph, take=0)
    state = State.load(project, "g")
    assert len(state.nodes["up"]) == 3  # one instance per item


def test_product_fanout(collections_project: Project):
    from smoketree.executor import run
    from smoketree.cache import State

    p = collections_project
    (p.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  ca: {type: collection, glob: sources/a/*.txt}\n"
        "  cb: {type: collection, glob: sources/b/*.txt}\n"
        "  paired: {type: transform, transformer: pair, "
        "inputs: {a: ca, b: cb}, expand: product}\n"
    )
    graph = load_graph(p, "g")
    run(p, graph, take=0)
    state = State.load(p, "g")
    assert len(state.nodes["paired"]) == 6  # 2 x 3


def test_zip_fanout_and_mismatch(collections_project: Project):
    from smoketree.executor import run
    from smoketree.cache import State

    p = collections_project
    # zip with equal lengths: a (2) zipped with a copy of a (2)
    for n in ("1", "2"):
        _write(p.sources_dir / "c" / f"{n}.txt", f"c{n}\n")
    (p.graphs_dir / "ok.yaml").write_text(
        "name: ok\n"
        "nodes:\n"
        "  ca: {type: collection, glob: sources/a/*.txt}\n"
        "  cc: {type: collection, glob: sources/c/*.txt}\n"
        "  z: {type: transform, transformer: pair, inputs: {a: ca, b: cc}, expand: zip}\n"
    )
    run(p, load_graph(p, "ok"), take=0)
    assert len(State.load(p, "ok").nodes["z"]) == 2

    # zip with unequal lengths -> runtime error
    (p.graphs_dir / "bad.yaml").write_text(
        "name: bad\n"
        "nodes:\n"
        "  ca: {type: collection, glob: sources/a/*.txt}\n"
        "  cb: {type: collection, glob: sources/b/*.txt}\n"
        "  z: {type: transform, transformer: pair, inputs: {a: ca, b: cb}, expand: zip}\n"
    )
    with pytest.raises(SmoketreeError, match="equal-length"):
        run(p, load_graph(p, "bad"), take=0)


def test_expand_required_when_collection_input(project: Project):
    _write(project.sources_dir / "a" / "1.txt", "a1\n")
    (project.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  items: {type: collection, glob: sources/a/*.txt}\n"
        "  up: {type: transform, transformer: shout, inputs: {input: items}}\n"
    )
    with pytest.raises(ValidationError, match="does not declare 'expand'"):
        load_graph(project, "g")


def test_expand_forbidden_without_collection(project: Project):
    (project.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  t: {type: source, path: sources/hello.txt}\n"
        "  up: {type: transform, transformer: shout, inputs: {input: t}, expand: each}\n"
    )
    with pytest.raises(ValidationError, match="no collection inputs"):
        load_graph(project, "g")


def test_each_rejects_multiple_collections(collections_project: Project):
    p = collections_project
    (p.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  ca: {type: collection, glob: sources/a/*.txt}\n"
        "  cb: {type: collection, glob: sources/b/*.txt}\n"
        "  m: {type: transform, transformer: pair, inputs: {a: ca, b: cb}, expand: each}\n"
    )
    with pytest.raises(ValidationError, match="requires exactly one"):
        load_graph(p, "g")


def test_empty_glob_is_error(project: Project):
    from smoketree.executor import run

    (project.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  items: {type: collection, glob: sources/none/*.txt}\n"
        "  up: {type: transform, transformer: shout, inputs: {input: items}, "
        "expand: each}\n"
    )
    with pytest.raises(SmoketreeError, match="matched no files"):
        run(project, load_graph(project, "g"), take=0)


def test_instance_hash_stable_and_distinct():
    h1 = cachelib.instance_hash({"a": "/p/1.txt", "b": "/p/x.txt"})
    h2 = cachelib.instance_hash({"b": "/p/x.txt", "a": "/p/1.txt"})  # order-independent
    h3 = cachelib.instance_hash({"a": "/p/2.txt", "b": "/p/x.txt"})
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 12


# --------------------------------------------------------------------------- #
# Tagged collections + filter_tag (Addendum 2)
# --------------------------------------------------------------------------- #


def test_input_ref_shorthand_equals_longform():
    from smoketree.graph import InputRef
    from smoketree.models import InputDecl

    short = InputRef.parse("references[subject]")
    long = InputRef.parse(InputDecl(node="references", filter_tag="subject"))
    assert short == long
    assert short.node_id == "references"
    assert short.filter_tag == "subject"
    assert short.output_name is None
    # plain string still works
    assert InputRef.parse("source").filter_tag is None


def _tagged_refs_graph(project: Project, body: str) -> None:
    for n in ("sub1", "sub2", "sty"):
        _write(project.sources_dir / "refs" / f"{n}.txt", f"{n}\n")
    (project.transformers_dir / "pair.yaml").write_text(_PAIR_TRANSFORMER)
    (project.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  references:\n"
        "    type: collection\n"
        "    sources:\n"
        "      - {path: sources/refs/sub1.txt, tags: [subject, primary]}\n"
        "      - {path: sources/refs/sub2.txt, tags: [subject]}\n"
        "      - {path: sources/refs/sty.txt, tags: [style]}\n"
        f"{body}"
    )


def test_filter_multi_fans_out(project: Project):
    from smoketree.executor import run
    from smoketree.cache import State

    _tagged_refs_graph(
        project,
        "  prep: {type: transform, transformer: shout, "
        "inputs: {input: 'references[subject]'}, expand: each}\n",
    )
    graph = load_graph(project, "g")
    assert graph.is_collection("prep") is True
    run(project, graph, take=0)
    assert len(State.load(project, "g").nodes["prep"]) == 2  # two subject items


def test_filter_single_is_scalar(project: Project):
    from smoketree.executor import run

    _tagged_refs_graph(
        project,
        "  prep: {type: transform, transformer: shout, "
        "inputs: {input: 'references[style]'}}\n",  # 1 item -> scalar, no expand
    )
    graph = load_graph(project, "g")
    assert graph.is_collection("prep") is False  # single match -> scalar
    run(project, graph, take=0)
    # flat (non-instance) cache layout for a scalar node
    assert (cachelib.cache_node_dir(project, "g", "prep", 0) / "text.txt").exists()


def test_filter_scalar_broadcasts_into_fanout(project: Project):
    from smoketree.executor import run
    from smoketree.cache import State

    _tagged_refs_graph(
        project,
        "  prep: {type: transform, transformer: shout, "
        "inputs: {input: 'references[subject]'}, expand: each}\n"
        "  combo: {type: transform, transformer: pair, "
        "inputs: {a: prep, b: 'references[style]'}, expand: each}\n",
    )
    graph = load_graph(project, "g")
    run(project, graph, take=0)
    # 2 subjects prepped, style broadcast -> 2 combo instances
    assert len(State.load(project, "g").nodes["combo"]) == 2


def test_filter_zero_match_errors(project: Project):
    from smoketree.executor import run

    _tagged_refs_graph(
        project,
        "  prep: {type: transform, transformer: shout, "
        "inputs: {input: 'references[nope]'}}\n",
    )
    with pytest.raises(SmoketreeError, match="matched no items"):
        run(project, load_graph(project, "g"), take=0)


def test_filter_tag_on_noncollection_is_parse_error(project: Project):
    (project.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  src: {type: source, path: sources/hello.txt}\n"
        "  p: {type: transform, transformer: shout, inputs: {input: 'src[x]'}}\n"
    )
    with pytest.raises(ValidationError, match="filter_tag"):
        load_graph(project, "g")


def test_collection_glob_and_sources_mutually_exclusive(project: Project):
    (project.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  c:\n"
        "    type: collection\n"
        "    glob: sources/*.txt\n"
        "    sources: [{path: sources/hello.txt}]\n"
        "  p: {type: transform, transformer: shout, inputs: {input: c}, expand: each}\n"
    )
    with pytest.raises(ValidationError, match="exactly one of 'glob' or 'sources'"):
        load_graph(project, "g")


def test_named_inputs_without_tagging(project: Project):
    """Fixed named scalar source inputs feeding a multi-input transform."""
    from smoketree.executor import run

    _write(project.sources_dir / "x.txt", "x\n")
    _write(project.sources_dir / "y.txt", "y\n")
    (project.transformers_dir / "pair.yaml").write_text(_PAIR_TRANSFORMER)
    (project.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  sx: {type: source, path: sources/x.txt}\n"
        "  sy: {type: source, path: sources/y.txt}\n"
        "  joined: {type: transform, transformer: pair, inputs: {a: sx, b: sy}}\n"
    )
    graph = load_graph(project, "g")
    assert graph.is_collection("joined") is False
    run(project, graph, take=0)
    out = (cachelib.cache_node_dir(project, "g", "joined", 0) / "text.txt").read_text()
    assert out == "x\ny\n"


def test_latent_media_type_validates(project: Project):
    (project.transformers_dir / "enc.yaml").write_text(
        "name: enc\n"
        "type: comfyui\n"
        "workflow: enc.json\n"
        "inputs:\n"
        "  image: {type: file, media: image, inject: {node_id: '4', field: image}}\n"
        "outputs:\n"
        "  latent: {type: file, media: latent, format: latent, "
        "collect: {node_id: '8', field: filename_prefix}}\n"
    )
    (project.sources_dir / "pic.png").write_bytes(b"\x89PNG\r\n")
    (project.graphs_dir / "g.yaml").write_text(
        "name: g\n"
        "nodes:\n"
        "  src: {type: source, path: sources/pic.png}\n"
        "  enc: {type: transform, transformer: enc, inputs: {image: src}}\n"
    )
    graph = load_graph(project, "g")  # latent output validates without error
    assert graph.transformers["enc"].outputs["latent"].media == "latent"
