function paint(state) {
  document.getElementById('security-state').innerHTML = `
    <div><span class="k">Listening on</span><b>${escapeHtml(state.bind)}:${state.port}</b></div>
    <div><span class="k">Password</span><b>${state.has_password ? 'set' : 'not set'}</b></div>
    <div><span class="k">Remote access</span><b>${state.remote_access ? 'on' : 'off'}</b></div>`;
  document.getElementById('current-wrap').hidden = !state.has_password;
  const remote = document.getElementById('remote');
  remote.checked = state.remote_access;
  remote.disabled = !state.has_password;
  document.getElementById('remote-help').textContent = state.has_password
    ? 'Takes effect when the panel restarts.'
    : 'Set a password first — this stays off until you do.';
}

function paintTls(state) {
  const t = (state && state.tls) || {};
  const box = document.getElementById('tls-state');
  const certEl = document.getElementById('tls-cert');
  const keyEl = document.getElementById('tls-key');
  if (certEl) certEl.value = t.cert || '';
  if (keyEl) keyEl.value = t.key || '';
  if (!box) return;
  if (!t.configured) {
    box.innerHTML = `<div><span class="k">HTTPS</span><b>off</b></div>
      <div><span class="k"></span><span class="dim">the panel is served over
        plain HTTP</span></div>`;
    return;
  }
  if (!t.valid) {
    box.innerHTML = `<div><span class="k">HTTPS</span><b class="err-inline">not usable</b></div>
      <div><span class="k"></span><span>${escapeHtml(t.error || '')}</span></div>`;
    return;
  }
  box.innerHTML = `<div><span class="k">HTTPS</span><b>ready</b></div>
    <div><span class="k">Issued to</span><b>${escapeHtml(t.subject || '—')}</b></div>
    <div><span class="k">Valid until</span><b>${escapeHtml(t.expires || '—')}</b></div>
    <div><span class="k">Covers</span><span>${(t.names || []).map(escapeHtml).join(', ') || '—'}</span></div>`;
}

/* Bound defensively. Static files are re-read from disk on every request but
   the page markup is baked into the imported module, so a panel restarted at
   the same moment a page changed can serve NEW script against OLD html. The
   script then throws on the first missing element and every handler after it
   silently never registers — which looks exactly like a button that does
   nothing. Reported as "I hit save hostnames and nothing happened". */
function on(id, event, fn) {
  const el = document.getElementById(id);
  if (el) el.addEventListener(event, fn);
  else console.warn(`security.js: #${id} is missing — the page and script may be out of step; restart the panel`);
}

on('tls-form', 'submit', e => {
  e.preventDefault();
  post('/api/security', {
    tls_cert: (document.getElementById('tls-cert') || {}).value || '',
    tls_key: (document.getElementById('tls-key') || {}).value || '',
  }).then(r => {
    toast(r.ok ? (r.data.message || 'Saved.') : r.data.error, r.ok ? 'good' : 'bad');
    if (r.ok) { paint(r.data.state); paintTls(r.data.state); }
  }).catch(() => {});
});

function paintHosts(state) {
  const input = document.getElementById('extra-hosts');
  if (input) input.value = (state.extra_hosts || []).join(', ');
  const box = document.getElementById('known-hosts');
  if (box) {
    box.innerHTML = `Already accepted without configuration:
      <code>${(state.known_hosts || []).map(escapeHtml).join('</code> <code>')}</code>`;
  }
}

on('hosts-form', 'submit', e => {
  e.preventDefault();
  post('/api/security', {
    extra_hosts: (document.getElementById('extra-hosts') || {}).value || ''
  }).then(r => {
    toast(r.ok ? (r.data.message || 'Saved.') : r.data.error, r.ok ? 'good' : 'bad');
    if (r.ok) { paint(r.data.state); paintHosts(r.data.state); paintTls(r.data.state); }
  }).catch(() => {});
});

on('password-form', 'submit', e => {
  e.preventDefault();
  post('/api/security', {
    password: document.getElementById('password').value,
    current: document.getElementById('current').value
  }).then(r => {
    toast(r.ok ? r.data.message : r.data.error, r.ok ? 'good' : 'bad');
    if (r.ok) {
      document.getElementById('password').value = '';
      document.getElementById('current').value = '';
      paint(r.data.state);
    }
  }).catch(() => {});
});

on('remote', 'change', e => {
  post('/api/security', { remote_access: e.target.checked }).then(r => {
    toast(r.ok ? r.data.message : r.data.error, r.ok ? 'good' : 'bad');
    if (r.ok) { paint(r.data.state); paintHosts(r.data.state); paintTls(r.data.state); }
    else e.target.checked = !e.target.checked;
  }).catch(() => {});
});

api('/api/security').then(d => { paint(d); paintHosts(d); paintTls(d); }).catch(() => {});
