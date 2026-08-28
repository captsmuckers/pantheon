/* Shared helpers. Every mutating request goes through post(), which is the one
   place the CSRF header is attached — a fetch that forgot it would be refused
   by the server, so this exists to make forgetting impossible rather than to
   provide the protection itself. */

function api(path) {
  return fetch(path, { headers: { 'Accept': 'application/json' } })
    .then(r => r.json());
}

function post(path, body) {
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Athena-CSRF': '1' },
    body: JSON.stringify(body || {})
  }).then(async r => {
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
    return { status: r.status, ok: r.ok, data };
  });
}

let toastTimer = null;
function toast(message, kind) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = message;
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'bad' ? 8000 : 4500);
}

/* Reveal toggles for password-style fields. The value is only ever in the
   input; nothing here reads it back out to anywhere. */
document.addEventListener('click', e => {
  const btn = e.target.closest('.eye');
  if (!btn) return;
  const input = document.getElementById(btn.dataset.for) ||
                btn.parentElement.querySelector('input');
  if (!input) return;
  const showing = input.type === 'text';
  input.type = showing ? 'password' : 'text';
  btn.textContent = showing ? 'show' : 'hide';
  btn.setAttribute('aria-label', showing ? 'Show' : 'Hide');
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
