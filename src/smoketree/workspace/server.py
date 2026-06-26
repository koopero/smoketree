"""The local web app behind ``smoketree workspace`` (path core).

A FastAPI app serving a JSON API plus a small no-build Vue frontend (static files
under ``web/``, Vue 3 vendored — no Node toolchain): a card grid of every rendered
output whose rule declares one or more ``feedback`` channels, each rendered as a
notes box or a select control that saves to the channel file, plus a Run button
that re-runs the pipeline (folding the new feedback) and streams progress — closing
the loop in the browser.

``fastapi``/``uvicorn`` are optional (the ``workspace`` extra); imported lazily.
"""

from __future__ import annotations

import difflib
import hashlib
import queue
import threading
import webbrowser
from pathlib import Path

_WEB_DIR = Path(__file__).parent / "web"

from pydantic import BaseModel

from .. import reconcile as reconcilelib
from ..errors import SmoketreeError
from ..project import Project
from ..rules import load_pipeline
from .actions import fire_trigger
from .channels import read_channels
from .graph import build_graph
from .index import add_note, build_index, set_select


class NoteIn(BaseModel):
    id: str
    channel: str
    text: str


class SelectIn(BaseModel):
    id: str
    channel: str
    value: str


class ReconcileIn(BaseModel):
    id: str
    action: str  # "merge" | "take-generated" | "keep-mine"


class RerollIn(BaseModel):
    id: str


class TriggerIn(BaseModel):
    rule: str


class RunIn(BaseModel):
    # Optional narrowing for "run this cell / rule"; absent ⇒ full run.
    only: list[str] | None = None
    where: dict[str, str] | None = None


def _drift_json(drift, root) -> dict:
    if drift.is_text:
        base = drift.base.read_text().splitlines() if drift.base.exists() else []
        generated = drift.template.read_text().splitlines()
        diff = "\n".join(
            difflib.unified_diff(base, generated, "forked", "generated", lineterm="")
        )
    else:
        diff = "(binary file)"
    return {
        "id": str(drift.authored.relative_to(root)),
        "label": drift.label,
        "edited": drift.copy_edited,
        "is_text": drift.is_text,
        "diff": diff[:4000],
    }


def _require_fastapi():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        raise SmoketreeError(
            "The workspace needs the 'workspace' extra. Install it with:\n"
            "    uv pip install 'smoketree[workspace]'"
        ) from exc


def _channels_json(channels) -> list[dict]:
    # A notes channel's *text* is never sent (the box is write-only); only `has_note` is
    # exposed for the highlight. A select channel sends its current value + options.
    out = []
    for c in channels:
        cj = {"name": c.name, "kind": c.kind, "describe": c.describe}
        if c.kind == "select":
            cj["options"] = c.options
            cj["value"] = c.value
            cj["default"] = c.default
        else:
            cj["has_note"] = c.has_note
        out.append(cj)
    return out


def _card_json(card) -> dict:
    return {
        "id": card.id,
        "rule": card.rule,
        "label": card.label,
        "media": card.media,
        "flagged": card.flagged,
        "reroll": card.reroll,
        "channels": _channels_json(card.channels),
        "artifact_url": f"/artifact?id={card.id}",
    }


def _file_url(rel: str) -> str:
    from urllib.parse import quote

    return f"/file?path={quote(rel)}"


def _thumbnail(src: Path, cache_dir: Path, max_edge: int = 400) -> "Path | None":
    """A cached, downscaled JPEG of an image (Pillow). Keyed by the source's mtime+size so a
    regenerated artifact gets a fresh thumb and an unchanged one is served from cache. Returns
    None when no thumbnail can be made (not an image, or Pillow unavailable) — the caller then
    serves the original. This keeps the workspace grid from decoding many full-res PNGs at once
    (the OOM that could crash a browser tab)."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return None
    try:
        st = src.stat()
    except OSError:
        return None
    key = hashlib.sha256(
        f"{src}:{st.st_mtime_ns}:{st.st_size}:{max_edge}".encode()
    ).hexdigest()[:24]
    out = cache_dir / f"{key}.jpg"
    if out.exists():
        return out
    try:
        with Image.open(src) as im:
            im.thumbnail((max_edge, max_edge))  # downscale-only, preserves aspect
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp")
            im.save(tmp, format="JPEG", quality=80)
            tmp.replace(out)  # atomic publish
    except Exception:
        return None
    return out


def _flagged(channels) -> bool:
    return any(
        (c.has_note if c.kind == "notes" else c.value != c.default) for c in channels
    )


def _instance_json(inst) -> dict:
    primary = inst.primary
    return {
        "identity": inst.identity,
        "rule": inst.rule,
        "keys": inst.keys,
        "label": inst.label,
        "state": inst.state,
        "reason": inst.reason,
        "media": primary.media if primary else None,
        "artifact_url": _file_url(primary.rel) if primary else None,
        "exists": primary.exists if primary else False,
        "outputs": [
            {"port": o.port, "rel": o.rel, "media": o.media,
             "exists": o.exists, "is_dir": o.is_dir}
            for o in inst.outputs
        ],
        "channels": _channels_json(inst.channels),
        "flagged": _flagged(inst.channels),
        "reroll": inst.reroll,
        "completed_at": inst.completed_at,
    }


def _graph_json(graph) -> dict:
    return {
        "pipeline": graph.pipeline,
        "rules": [
            {
                "name": r.name,
                "enabled": r.enabled,
                "deps": r.deps,
                "has_feedback": r.has_feedback,
                "trigger": {"describe": r.trigger.describe} if r.trigger else None,
                "state": r.state,
                "reason": r.reason,
                "instances": [_instance_json(i) for i in r.instances],
            }
            for r in graph.rules
        ],
    }


def create_app(project: Project, pipeline_id: str):
    _require_fastapi()
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import (
        FileResponse,
        JSONResponse,
        StreamingResponse,
    )
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title=f"smoketree workspace — {pipeline_id}")
    app.mount("/assets", StaticFiles(directory=_WEB_DIR), name="assets")
    run_lock = threading.Lock()
    root = project.root

    def _index():
        return build_index(Project(root), pipeline_id)

    def _find_card(card_id: str):
        for card in _index():
            if card.id == card_id:
                return card
        raise HTTPException(status_code=404, detail="Unknown output.")

    def _find_channel(card_id: str, channel_name: str):
        # Fast path: the rule's output exists, so it's a card in the feedback index.
        for card in _index():
            if card.id == card_id:
                for channel in card.channels:
                    if channel.name == channel_name:
                        return channel
                raise HTTPException(status_code=404, detail="Unknown channel.")
        # Fallback: a feedback channel is a standalone file, so it's settable even before the
        # rule has produced its output (e.g. a GATE select shown in the graph view). Resolve
        # it straight from the rule + keys encoded in the id.
        channel = _channel_from_id(card_id, channel_name)
        if channel is not None:
            return channel
        raise HTTPException(status_code=404, detail="Unknown output.")

    def _channel_from_id(card_id: str, channel_name: str):
        """Resolve a feedback channel from a `<rule>:<k=v,…>` id without needing its output."""
        rule_name, _, keypart = card_id.partition(":")
        if not rule_name:
            return None
        keys = dict(
            kv.split("=", 1) for kv in keypart.split(",") if "=" in kv
        )
        loaded = load_pipeline(Project(root), pipeline_id)
        rule = next((r for r in loaded.rules if r.name == rule_name), None)
        if rule is None or not rule.feedback:
            return None
        for ch in read_channels(root, rule, keys):
            if ch.name == channel_name:
                return ch
        return None

    def _guard_in_project(path) -> None:
        if not str(path.resolve()).startswith(str(project.root.resolve())):
            raise HTTPException(status_code=400, detail="Refusing path outside project.")

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html")

    @app.get("/api/meta")
    def api_meta() -> JSONResponse:
        return JSONResponse({"pipeline": pipeline_id})

    @app.get("/api/index")
    def api_index() -> JSONResponse:
        try:
            cards = [_card_json(c) for c in _index()]
        except SmoketreeError as exc:
            return JSONResponse({"pipeline": pipeline_id, "cards": [], "error": str(exc)})
        return JSONResponse({"pipeline": pipeline_id, "cards": cards})

    @app.get("/api/graph")
    def api_graph() -> JSONResponse:
        try:
            graph = build_graph(Project(root), pipeline_id)
        except SmoketreeError as exc:
            return JSONResponse({"pipeline": pipeline_id, "rules": [], "error": str(exc)})
        return JSONResponse(_graph_json(graph))

    def _svg_media(path: Path) -> str | None:
        # Browsers won't render an <img> whose response lacks an SVG MIME type, and the
        # system mimetypes DB doesn't always register .svg — set it explicitly.
        return "image/svg+xml" if path.suffix.lower() == ".svg" else None

    @app.get("/artifact")
    def artifact(id: str) -> FileResponse:
        card = _find_card(id)
        if not card.output_path.exists():
            raise HTTPException(status_code=404, detail="No output for that instance.")
        return FileResponse(card.output_path, media_type=_svg_media(card.output_path))

    @app.get("/file")
    def file(path: str) -> FileResponse:
        target = (root / path).resolve()
        if not str(target).startswith(str(project.root.resolve())):
            raise HTTPException(status_code=400, detail="Refusing path outside project.")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="No such file.")
        return FileResponse(target, media_type=_svg_media(target))

    @app.get("/thumb")
    def thumb(id: str | None = None, path: str | None = None) -> FileResponse:
        """A small cached thumbnail of an image artifact (by card id or path) so the grid
        loads tiny JPEGs instead of many full-res PNGs. Falls back to the original file when
        it isn't a rasterable image (SVG, etc.)."""
        if id is not None:
            src = _find_card(id).output_path
        elif path is not None:
            src = (root / path).resolve()
            if not str(src).startswith(str(project.root.resolve())):
                raise HTTPException(status_code=400, detail="Refusing path outside project.")
        else:
            raise HTTPException(status_code=400, detail="thumb needs an id or path.")
        if not src.is_file():
            raise HTTPException(status_code=404, detail="No such file.")
        thumb_path = _thumbnail(src, project.root / ".smoketree" / "thumbs")
        if thumb_path is None:  # not a raster image — serve the original
            return FileResponse(src, media_type=_svg_media(src))
        return FileResponse(thumb_path, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.post("/api/note")
    def post_note(note: NoteIn) -> JSONResponse:
        channel = _find_channel(note.id, note.channel)
        if channel.kind != "notes":
            raise HTTPException(status_code=400, detail="Channel is not a notes channel.")
        _guard_in_project(channel.path)
        has_note = add_note(channel, note.text)
        return JSONResponse({"ok": True, "has_note": has_note})

    @app.post("/api/select")
    def post_select(sel: SelectIn) -> JSONResponse:
        channel = _find_channel(sel.id, sel.channel)
        if channel.kind != "select":
            raise HTTPException(status_code=400, detail="Channel is not a select channel.")
        _guard_in_project(channel.path)
        try:
            value = set_select(channel, sel.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "value": value})

    @app.get("/api/drift")
    def api_drift() -> JSONResponse:
        try:
            loaded = load_pipeline(Project(root), pipeline_id)
            drifts = reconcilelib.find_drift(Project(root), loaded)
        except SmoketreeError as exc:
            return JSONResponse({"drift": [], "error": str(exc)})
        return JSONResponse({"drift": [_drift_json(d, root) for d in drifts]})

    @app.post("/api/reroll")
    def post_reroll(req: RerollIn) -> JSONResponse:
        card = _find_card(req.id)
        if not card.reroll:
            raise HTTPException(status_code=400, detail="Output's rule has no reroll: true.")
        roll = card.output_path.with_name(card.output_path.name + ".roll")
        _guard_in_project(roll)
        try:
            current = int(roll.read_text().strip() or "0") if roll.exists() else 0
        except ValueError:
            current = 0
        roll.write_text(f"{current + 1}\n")
        return JSONResponse({"ok": True, "roll": current + 1})

    @app.post("/api/trigger")
    def post_trigger(req: TriggerIn) -> JSONResponse:
        loaded = load_pipeline(Project(root), pipeline_id)
        rule = next((r for r in loaded.rules if r.name == req.rule), None)
        if rule is None:
            raise HTTPException(status_code=404, detail="Unknown rule.")
        if rule.trigger is None:
            raise HTTPException(status_code=400, detail="Rule has no trigger.")
        try:
            marker = fire_trigger(Project(root), rule)
        except SmoketreeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "marker": str(marker.relative_to(root))})

    @app.post("/api/reconcile")
    def post_reconcile(req: ReconcileIn) -> JSONResponse:
        loaded = load_pipeline(Project(root), pipeline_id)
        for drift in reconcilelib.find_drift(Project(root), loaded):
            if str(drift.authored.relative_to(root)) == req.id:
                _guard_in_project(drift.authored)
                try:
                    status = reconcilelib.resolve(drift, req.action)
                except SmoketreeError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                return JSONResponse({"ok": True, "status": status})
        raise HTTPException(status_code=404, detail="No such drift.")

    @app.post("/api/run")
    def run_pipeline(req: RunIn | None = None) -> StreamingResponse:
        if not run_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        only = set(req.only) if req and req.only else None
        where = req.where if req else None
        q: queue.Queue = queue.Queue()
        done = object()

        def worker() -> None:
            try:
                from .. import engine
                from ..rules import load_pipeline

                p = Project(root)
                loaded = load_pipeline(p, pipeline_id)
                engine.run(p, loaded, only=only, where=where,
                           report=lambda line: q.put(str(line)))
                q.put("[OK] run complete")
            except Exception as exc:
                q.put(f"[ERROR] {exc}")
            finally:
                q.put(done)
                run_lock.release()

        threading.Thread(target=worker, daemon=True).start()

        def stream():
            while True:
                item = q.get()
                if item is done:
                    break
                yield item + "\n"

        return StreamingResponse(stream(), media_type="text/plain")

    return app


def serve(
    project: Project,
    pipeline_id: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Run the workspace server (blocking until Ctrl-C)."""
    _require_fastapi()
    import uvicorn

    app = create_app(project, pipeline_id)
    url = f"http://{host}:{port}/"
    print(f"smoketree workspace for '{pipeline_id}' → {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
