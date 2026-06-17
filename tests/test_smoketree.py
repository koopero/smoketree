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
    init_project(tmp_path, "test-proj")
    return Project(tmp_path)


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


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


def test_portrait_topological_order(project: Project):
    graph = load_graph(project, "portrait")
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
    (project.graphs_dir / "mm.yaml").write_text(
        "name: mm\n"
        "nodes:\n"
        "  t: {type: source, path: sources/hello.txt}\n"
        "  b: {type: transform, transformer: describe, inputs: {image: t}}\n"
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
