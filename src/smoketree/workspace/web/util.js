// Small fetch + escaping helpers shared across components.

export async function postJSON(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// The feedback-card id (rule:k=v,… with keys sorted) that /api/note & /api/select key by.
// Reconstructed client-side from a graph instance so the detail pane's channels work.
export function cardId(inst) {
  const slug = Object.keys(inst.keys).sort()
    .map((k) => `${k}=${inst.keys[k]}`).join(',');
  return `${inst.rule}:${slug}`;
}
