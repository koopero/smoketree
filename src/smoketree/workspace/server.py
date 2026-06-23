"""The local web app behind ``smoketree workspace`` (path core).

A tiny FastAPI app (no build step, vanilla HTML/JS): a card grid of every rendered output
whose rule declares one or more ``feedback`` channels, each rendered as a notes box or a
select control that saves to the channel file, plus a Run button that re-runs the pipeline
(folding the new feedback) and streams progress — closing the loop in the browser.

``fastapi``/``uvicorn`` are optional (the ``workspace`` extra); imported lazily.
"""

from __future__ import annotations

import difflib
import queue
import threading
import webbrowser

from pydantic import BaseModel

from .. import reconcile as reconcilelib
from ..errors import SmoketreeError
from ..project import Project
from ..rules import load_pipeline
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


def _card_json(card) -> dict:
    # A notes channel's *text* is never sent (the box is write-only); only `has_note` is
    # exposed for the highlight. A select channel sends its current value + options.
    channels = []
    for c in card.channels:
        cj = {"name": c.name, "kind": c.kind, "describe": c.describe}
        if c.kind == "select":
            cj["options"] = c.options
            cj["value"] = c.value
            cj["default"] = c.default
        else:
            cj["has_note"] = c.has_note
        channels.append(cj)
    return {
        "id": card.id,
        "rule": card.rule,
        "label": card.label,
        "media": card.media,
        "flagged": card.flagged,
        "reroll": card.reroll,
        "channels": channels,
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

    def _find_channel(card_id: str, channel_name: str):
        card = _find_card(card_id)
        for channel in card.channels:
            if channel.name == channel_name:
                return channel
        raise HTTPException(status_code=404, detail="Unknown channel.")

    def _guard_in_project(path) -> None:
        if not str(path.resolve()).startswith(str(project.root.resolve())):
            raise HTTPException(status_code=400, detail="Refusing path outside project.")

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
  .card.flagged { border-color: #d8a657; }
  .card .head { padding: 10px 12px; border-bottom: 1px solid #2a2d34;
                display: flex; align-items: center; gap: 8px; }
  .card .node { font-weight: 600; }
  .card .label { color: #8b8f98; font-size: 12px; overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #3a3d44;
         flex: none; margin-left: auto; }
  .card.flagged .dot { background: #d8a657; }
  button.reroll { margin-left: auto; background: #2a2433; color: #d9c7ef;
                  border: 1px solid #4a3d5f; border-radius: 7px; padding: 4px 9px;
                  cursor: pointer; font: 600 11px/1 system-ui, sans-serif; }
  button.reroll:hover { border-color: #6a5a85; }
  button.reroll:disabled { opacity: 0.6; cursor: default; }
  .head button.reroll + .dot { margin-left: 8px; }
  .preview { background: #0f1115; min-height: 80px; display: flex;
             align-items: center; justify-content: center; }
  .preview img { max-width: 100%; max-height: 320px; display: block; }
  .preview video { max-width: 100%; max-height: 360px; display: block; }
  .preview pre { margin: 0; padding: 12px; max-height: 240px; overflow: auto;
                 white-space: pre-wrap; font-size: 12px; width: 100%; color: #c8ccd4; }
  .preview .none { color: #6b6f78; padding: 24px; font-size: 12px; }
  textarea { width: 100%; border: 1px solid #2a2d34; border-radius: 6px;
             background: #14161a; color: #e8e8ea; padding: 8px 10px;
             font: 13px/1.5 system-ui, sans-serif; resize: vertical; min-height: 56px; }
  textarea:focus { outline: none; border-color: #4a4f59; }
  .noterow { display: flex; align-items: center; gap: 10px; padding: 6px 0 0; }
  button.addnote { background: #262a33; color: #cdd2db; border: 1px solid #3a3d44;
                   border-radius: 7px; padding: 6px 12px; cursor: pointer;
                   font: 600 12px/1 system-ui, sans-serif; }
  button.addnote:hover { background: #2d323d; border-color: #4a4f59; }
  button.addnote:disabled { opacity: 0.5; cursor: default; }
  .saved { font-size: 11px; color: #7bbf7b; }
  .hint { font-size: 11px; color: #6b6f78; margin-left: auto; }
  .channels { display: flex; flex-direction: column; }
  .channel { border-top: 1px solid #2a2d34; padding: 9px 12px 11px; }
  .chead { font-size: 11px; font-weight: 600; color: #aab2bd; text-transform: uppercase;
           letter-spacing: .04em; margin-bottom: 6px; }
  .chead .desc { font-weight: 400; text-transform: none; letter-spacing: 0; color: #8b8f98; }
  .select { display: flex; flex-wrap: wrap; gap: 6px; }
  button.opt { background: #262a33; color: #cdd2db; border: 1px solid #3a3d44;
               border-radius: 999px; padding: 5px 12px; cursor: pointer;
               font: 600 12px/1 system-ui, sans-serif; }
  button.opt:hover { border-color: #4a4f59; }
  button.opt.active { background: #2f6f4f; color: #eafff2; border-color: #3a8a63; }
  .empty { color: #8b8f98; padding: 24px; }
  #drift:not(:empty) { margin: 18px 24px 0; border: 1px solid #d8a657;
                       border-radius: 10px; overflow: hidden; }
  #drift h2 { margin: 0; padding: 10px 14px; font-size: 13px; background: #2a230f;
              color: #e7c87a; border-bottom: 1px solid #3a3115; }
  .drow { padding: 10px 14px; border-bottom: 1px solid #2a2d34; }
  .drow:last-child { border-bottom: none; }
  .drow .top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .drow .path { font-weight: 600; }
  .drow .edited { font-size: 11px; color: #d8a657; }
  .drow .acts { margin-left: auto; display: flex; gap: 6px; }
  .drow button { background: #262a33; color: #cdd2db; border: 1px solid #3a3d44;
                 border-radius: 7px; padding: 5px 10px; cursor: pointer;
                 font: 600 12px/1 system-ui, sans-serif; }
  .drow button:hover { border-color: #4a4f59; }
  .drow pre { margin: 8px 0 0; padding: 8px 10px; background: #0f1115; border-radius: 6px;
              max-height: 200px; overflow: auto; font: 12px/1.45 ui-monospace, monospace; }
  .drow .add { color: #7bbf7b; } .drow .del { color: #d98a8a; }
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
<section id="drift"></section>
<main id="grid"><div class="empty">Loading…</div></main>
<script>
const grid = document.getElementById('grid');
const runBtn = document.getElementById('run');
const logEl = document.getElementById('log');
const driftEl = document.getElementById('drift');
let bust = Date.now();

function colorDiff(diff) {
  return diff.split('\\n').map(l => {
    const e = esc(l);
    if (l.startsWith('+') && !l.startsWith('+++')) return `<span class="add">${e}</span>`;
    if (l.startsWith('-') && !l.startsWith('---')) return `<span class="del">${e}</span>`;
    return e;
  }).join('\\n');
}

async function renderDrift() {
  const data = await (await fetch('/api/drift')).json();
  if (!data.drift || !data.drift.length) { driftEl.innerHTML = ''; return; }
  let html = `<h2>⚠ ${data.drift.length} authored cop(ies) drifted from their template</h2>`;
  for (const d of data.drift) {
    html += `<div class="drow" data-id="${esc(d.id)}">
      <div class="top">
        <span class="path">${esc(d.id)}</span>
        ${d.edited ? '<span class="edited">you edited it</span>' : ''}
        <span class="acts">
          ${d.is_text ? '<button data-a="merge" type="button">merge</button>' : ''}
          <button data-a="take-generated" type="button">take generated</button>
          <button data-a="keep-mine" type="button">keep mine</button>
        </span>
      </div>
      <pre>${colorDiff(d.diff)}</pre>
    </div>`;
  }
  driftEl.innerHTML = html;
  driftEl.querySelectorAll('.drow').forEach(row => {
    const id = row.getAttribute('data-id');
    row.querySelectorAll('button').forEach(b => b.addEventListener('click', async () => {
      b.disabled = true;
      const r = await fetch('/api/reconcile', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id, action: b.getAttribute('data-a')}),
      });
      if (r.ok) { await renderDrift(); await render(); } else { b.disabled = false; }
    }));
  });
}

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

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function isFlagged(card) {
  return card.channels.some(c => c.kind === 'notes' ? c.has_note : c.value !== c.default);
}
function updateFlag(card) { card.cardEl.classList.toggle('flagged', isFlagged(card)); }

function chHead(ch) {
  return `<div class="chead">${esc(ch.name)}` +
    (ch.describe ? ` — <span class="desc">${esc(ch.describe)}</span>` : '') + `</div>`;
}

function notesChannel(card, ch) {
  // Write-only append box: submit explicitly (button or ⌘/Ctrl+Enter), then clear.
  const el = document.createElement('div');
  el.className = 'channel';
  el.innerHTML = chHead(ch) + `
    <textarea placeholder="Add a note…"></textarea>
    <div class="noterow">
      <button class="addnote" type="button">Add note</button>
      <span class="saved"></span><span class="hint">⌘/Ctrl + Enter</span>
    </div>`;
  const textarea = el.querySelector('textarea');
  const btn = el.querySelector('.addnote');
  const savedEl = el.querySelector('.saved');
  const add = async () => {
    if (!textarea.value.trim()) return;
    btn.disabled = true;
    try {
      const r = await fetch('/api/note', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: card.id, channel: ch.name, text: textarea.value}),
      });
      const j = await r.json();
      textarea.value = ''; ch.has_note = j.has_note; updateFlag(card);
      savedEl.textContent = 'added';
      setTimeout(() => { savedEl.textContent = ''; }, 1500);
    } finally { btn.disabled = false; }
  };
  btn.addEventListener('click', add);
  textarea.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); add(); }
  });
  return el;
}

function selectChannel(card, ch) {
  const el = document.createElement('div');
  el.className = 'channel';
  el.innerHTML = chHead(ch) + `<div class="select">` +
    ch.options.map(o =>
      `<button class="opt${o === ch.value ? ' active' : ''}" type="button" ` +
      `data-v="${esc(o)}">${esc(o)}</button>`).join('') + `</div>`;
  el.querySelectorAll('.opt').forEach(b => b.addEventListener('click', async () => {
    const v = b.getAttribute('data-v');
    const r = await fetch('/api/select', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: card.id, channel: ch.name, value: v}),
    });
    if (!r.ok) return;
    ch.value = v;
    el.querySelectorAll('.opt').forEach(x =>
      x.classList.toggle('active', x.getAttribute('data-v') === v));
    updateFlag(card);
  }));
  return el;
}

function channelEl(card, ch) {
  return ch.kind === 'select' ? selectChannel(card, ch) : notesChannel(card, ch);
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
    el.className = 'card' + (card.flagged ? ' flagged' : '');
    card.cardEl = el;
    el.innerHTML = `
      <div class="head">
        <span class="node">${esc(card.rule)}</span>
        <span class="label">${esc(card.label)}</span>
        ${card.reroll ? '<button class="reroll" type="button">🎲 re-roll</button>' : ''}
        <span class="dot"></span>
      </div>
      <div class="preview">${await preview(card)}</div>
      <div class="channels"></div>`;
    const wrap = el.querySelector('.channels');
    for (const ch of card.channels) wrap.appendChild(channelEl(card, ch));
    const rb = el.querySelector('.reroll');
    if (rb) rb.addEventListener('click', async () => {
      rb.disabled = true;
      await fetch('/api/reroll', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: card.id}),
      });
      await runPipeline();  // re-render the bumped cell, then refresh
    });
    grid.appendChild(el);
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
    await renderDrift();
    runBtn.disabled = false; runBtn.textContent = label;
  }
}
runBtn.addEventListener('click', runPipeline);

render();
renderDrift();
</script>
</body>
</html>
"""
