"""The local web app behind ``smoketree workspace`` (path core).

A tiny FastAPI app (no build step, vanilla HTML/JS): a card grid of every rendered output
whose rule declares a ``feedback.append`` channel, each with a note box that saves to the
channel file, plus a Run button that re-runs the pipeline (folding the new feedback) and
streams progress — closing the human-in-the-loop cycle in the browser.

``fastapi``/``uvicorn`` are optional (the ``workspace`` extra); imported lazily.
"""

from __future__ import annotations

import queue
import threading
import webbrowser

from pydantic import BaseModel

from ..errors import SmoketreeError
from ..project import Project
from .index import add_note, build_index


class NoteIn(BaseModel):
    id: str
    text: str


def _require_fastapi():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        raise SmoketreeError(
            "The workspace needs the 'workspace' extra. Install it with:\n"
            "    uv pip install 'smoketree[workspace]'"
        ) from exc


def _card_json(card) -> dict:
    # Note the channel's *content* is never sent: the box is write-only (append). Only
    # `has_note` is exposed, to show the "noted" highlight.
    return {
        "id": card.id,
        "rule": card.rule,
        "label": card.label,
        "media": card.media,
        "has_note": card.has_note,
        "artifact_url": f"/artifact?id={card.id}",
    }


def create_app(project: Project, pipeline_id: str):
    _require_fastapi()
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import (
        FileResponse,
        HTMLResponse,
        JSONResponse,
        StreamingResponse,
    )

    app = FastAPI(title=f"smoketree workspace — {pipeline_id}")
    run_lock = threading.Lock()
    root = project.root

    def _index():
        return build_index(Project(root), pipeline_id)

    def _find_card(card_id: str):
        for card in _index():
            if card.id == card_id:
                return card
        raise HTTPException(status_code=404, detail="Unknown output.")

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return _PAGE.replace("__PIPELINE__", pipeline_id)

    @app.get("/api/index")
    def api_index() -> JSONResponse:
        try:
            cards = [_card_json(c) for c in _index()]
        except SmoketreeError as exc:
            return JSONResponse({"pipeline": pipeline_id, "cards": [], "error": str(exc)})
        return JSONResponse({"pipeline": pipeline_id, "cards": cards})

    @app.get("/artifact")
    def artifact(id: str) -> FileResponse:
        card = _find_card(id)
        if not card.output_path.exists():
            raise HTTPException(status_code=404, detail="No output for that instance.")
        return FileResponse(card.output_path)

    @app.post("/api/note")
    def post_note(note: NoteIn) -> JSONResponse:
        card = _find_card(note.id)
        path = card.note_path.resolve()
        if not str(path).startswith(str(project.root.resolve())):
            raise HTTPException(status_code=400, detail="Refusing path outside project.")
        has_note = add_note(card, note.text)
        return JSONResponse({"ok": True, "has_note": has_note})

    @app.post("/api/run")
    def run_pipeline() -> StreamingResponse:
        if not run_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        q: queue.Queue = queue.Queue()
        done = object()

        def worker() -> None:
            try:
                from .. import engine
                from ..rules import load_pipeline

                p = Project(root)
                loaded = load_pipeline(p, pipeline_id)
                engine.run(p, loaded, report=lambda line: q.put(str(line)))
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


# --------------------------------------------------------------------------- #
# Frontend (single page, no build step)
# --------------------------------------------------------------------------- #

_PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>smoketree workspace — __PIPELINE__</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 system-ui, sans-serif;
         background: #14161a; color: #e8e8ea; }
  header { padding: 16px 24px; border-bottom: 1px solid #2a2d34;
           position: sticky; top: 0; background: #14161a; z-index: 2;
           display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  header h1 { margin: 0; font-size: 16px; font-weight: 600; }
  header .sub { color: #8b8f98; font-size: 12px; margin-top: 2px; }
  button#run { background: #2f6f4f; color: #eafff2; border: 1px solid #3a8a63;
               border-radius: 8px; padding: 9px 16px; font: 600 13px/1 system-ui, sans-serif;
               cursor: pointer; white-space: nowrap; flex: none; }
  button#run:hover { background: #367c59; }
  button#run:disabled { opacity: 0.6; cursor: default; }
  #log { margin: 0; padding: 10px 24px; background: #0f1115;
         border-bottom: 1px solid #2a2d34; max-height: 200px; overflow: auto;
         white-space: pre-wrap; font: 12px/1.5 ui-monospace, monospace; color: #a7b6c2; }
  main { padding: 24px; display: grid; gap: 20px;
         grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }
  .card { background: #1c1f26; border: 1px solid #2a2d34; border-radius: 10px;
          overflow: hidden; display: flex; flex-direction: column; }
  .card.noted { border-color: #d8a657; }
  .card .head { padding: 10px 12px; border-bottom: 1px solid #2a2d34;
                display: flex; align-items: center; gap: 8px; }
  .card .node { font-weight: 600; }
  .card .label { color: #8b8f98; font-size: 12px; overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #3a3d44;
         flex: none; margin-left: auto; }
  .card.noted .dot { background: #d8a657; }
  .preview { background: #0f1115; min-height: 80px; display: flex;
             align-items: center; justify-content: center; }
  .preview img { max-width: 100%; max-height: 320px; display: block; }
  .preview video { max-width: 100%; max-height: 360px; display: block; }
  .preview pre { margin: 0; padding: 12px; max-height: 240px; overflow: auto;
                 white-space: pre-wrap; font-size: 12px; width: 100%; color: #c8ccd4; }
  .preview .none { color: #6b6f78; padding: 24px; font-size: 12px; }
  textarea { width: 100%; border: none; border-top: 1px solid #2a2d34;
             background: #1c1f26; color: #e8e8ea; padding: 10px 12px;
             font: 13px/1.5 system-ui, sans-serif; resize: vertical; min-height: 64px; }
  textarea:focus { outline: none; background: #21252e; }
  .noterow { display: flex; align-items: center; gap: 10px; padding: 8px 12px 12px; }
  button.addnote { background: #262a33; color: #cdd2db; border: 1px solid #3a3d44;
                   border-radius: 7px; padding: 6px 12px; cursor: pointer;
                   font: 600 12px/1 system-ui, sans-serif; }
  button.addnote:hover { background: #2d323d; border-color: #4a4f59; }
  button.addnote:disabled { opacity: 0.5; cursor: default; }
  .saved { font-size: 11px; color: #7bbf7b; }
  .hint { font-size: 11px; color: #6b6f78; margin-left: auto; }
  .empty { color: #8b8f98; padding: 24px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>smoketree workspace</h1>
    <div class="sub">pipeline <strong>__PIPELINE__</strong> · note an output, then Run to apply</div>
  </div>
  <button id="run" type="button">▶ Run pipeline</button>
</header>
<pre id="log" hidden></pre>
<main id="grid"><div class="empty">Loading…</div></main>
<script>
const grid = document.getElementById('grid');
const runBtn = document.getElementById('run');
const logEl = document.getElementById('log');
let bust = Date.now();

async function preview(card) {
  const url = `${card.artifact_url}&t=${bust}`;
  if (card.media === 'image') return `<img loading="lazy" src="${url}">`;
  if (card.media === 'video') return `<video src="${url}" controls loop muted playsinline></video>`;
  if (card.media === 'text' || card.media === 'data') {
    try {
      const t = await (await fetch(url)).text();
      return `<pre>${t.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</pre>`;
    } catch { return '<div class="none">could not load output</div>'; }
  }
  return `<div class="none"><a href="${url}">download ${card.media}</a></div>`;
}

function bindNote(el, card) {
  // Write-only append box: submit a new note explicitly (button or ⌘/Ctrl+Enter), then
  // clear for the next one. No save-on-blur. Never pre-filled with prior feedback.
  const textarea = el.querySelector('textarea');
  const btn = el.querySelector('.addnote');
  const savedEl = el.querySelector('.saved');
  const add = async () => {
    if (!textarea.value.trim()) return;
    btn.disabled = true;
    try {
      const r = await fetch('/api/note', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: card.id, text: textarea.value}),
      });
      const j = await r.json();
      textarea.value = '';
      card.cardEl.classList.toggle('noted', j.has_note);
      savedEl.textContent = 'added';
      setTimeout(() => { savedEl.textContent = ''; }, 1500);
    } finally { btn.disabled = false; }
  };
  btn.addEventListener('click', add);
  textarea.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); add(); }
  });
}

async function render() {
  const data = await (await fetch('/api/index')).json();
  if (data.error) { grid.innerHTML = '<div class="empty">⚠ ' + data.error + '</div>'; return; }
  if (!data.cards.length) {
    grid.innerHTML = '<div class="empty">No reviewable outputs yet. Run the pipeline first ' +
      '(its render rules need a <code>feedback:</code> channel).</div>';
    return;
  }
  grid.innerHTML = '';
  for (const card of data.cards) {
    const el = document.createElement('div');
    el.className = 'card' + (card.has_note ? ' noted' : '');
    card.cardEl = el;
    el.innerHTML = `
      <div class="head">
        <span class="node">${card.rule}</span>
        <span class="label">${card.label}</span>
        <span class="dot"></span>
      </div>
      <div class="preview">${await preview(card)}</div>
      <textarea placeholder="Add feedback…"></textarea>
      <div class="noterow">
        <button class="addnote" type="button">Add note</button>
        <span class="saved"></span>
        <span class="hint">⌘/Ctrl + Enter</span>
      </div>`;
    grid.appendChild(el);
    bindNote(el, card);
  }
}

async function runPipeline() {
  const label = runBtn.textContent;
  runBtn.disabled = true; runBtn.textContent = '… running';
  logEl.hidden = false; logEl.textContent = '';
  try {
    const resp = await fetch('/api/run', {method: 'POST'});
    if (resp.status === 409) { logEl.textContent = 'A run is already in progress.'; return; }
    const reader = resp.body.getReader(); const dec = new TextDecoder();
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      logEl.textContent += dec.decode(value, {stream: true});
      logEl.scrollTop = logEl.scrollHeight;
    }
  } catch (e) {
    logEl.textContent += '\\n[error] ' + e;
  } finally {
    bust = Date.now();
    await render();
    runBtn.disabled = false; runBtn.textContent = label;
  }
}
runBtn.addEventListener('click', runPipeline);

render();
</script>
</body>
</html>
"""
