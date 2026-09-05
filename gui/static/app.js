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
    headers: { 'Content-Type': 'application/json', 'X-Pantheon-CSRF': '1' },
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

/* Who is signed in, and a way to stop being them. The panel is shared with
   friends on a machine nobody locks, so "sign out" is not a formality. */
(function () {
  const label = document.getElementById('whoami');
  const btn = document.getElementById('signout');
  if (!label && !btn) return;                 // login page has no chrome
  api('/api/whoami').then(d => {
    if (!label || !d || !d.user) return;
    label.textContent = d.user + (d.role === 'admin' ? ' · administrator' : '');
  }).catch(() => {});
  if (btn) btn.addEventListener('click', () => {
    btn.disabled = true;
    post('/api/logout').then(() => { location.href = '/login'; })
      .catch(() => { btn.disabled = false; toast('Could not sign out.'); });
  });
})();

/* The shared-lab banner. One process serves every signed-in person, so the
   checkpoint someone else loads replaces yours without warning. Saying what is
   loaded and who loaded it turns a confusing failure into a visible fact. */
(function () {
  const bar = document.getElementById('labbar');
  if (!bar) return;
  const KINDS = { base: 'Clone a voice from a recording',
                  voicedesign: 'Describe the voice you want',
                  customvoice: "One of Qwen's 9 ready-made voices" };
  const ago = t => {
    if (!t) return '';
    const m = Math.max(0, Math.round((Date.now() / 1000 - t) / 60));
    return m < 1 ? 'just now' : m === 1 ? '1 minute ago' : m + ' minutes ago';
  };
  /* Who else is here matters more than what is loaded: the checkpoint can be
     reloaded, but only a person can be asked to wait. */
  const roster = w => {
    if (!w || !w.users || !w.users.length) return '';
    const others = w.users.filter(u => u.user !== w.me);
    if (!others.length) return '<span class="labbar-alone">only you</span>';
    return 'also here: ' + others.map(u =>
      `<strong>${escapeHtml(u.user)}</strong>` +
      (Date.now() / 1000 - u.seen > 300 ? ` (idle ${ago(u.seen)})` : '')
    ).join(', ');
  };
  const paint = () => Promise.all([
    api('/api/who').catch(() => null),
    api('/api/lab-state').catch(() => null)
  ]).then(([w, d]) => {
    const parts = [];
    const people = roster(w);
    if (people) parts.push(people);
    if (d && d.mode) {
      const kind = KINDS[d.mode] || d.mode;
      const who = d.user ? ` (loaded by <strong>${escapeHtml(d.user)}</strong> ${ago(d.at)})` : '';
      parts.push(`voice lab: <strong>${escapeHtml(kind)}</strong>${who}`);
      if (d.mode !== 'base') {
        parts.push('<span class="labbar-warn">/tts cloning unavailable while '
                 + 'this is loaded</span>');
      }
    }
    if (!parts.length) { bar.hidden = true; return; }
    bar.innerHTML = parts.join(' · ');
    bar.hidden = false;
  }).catch(() => {});
  paint();
  setInterval(paint, 15000);
})();
