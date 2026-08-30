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
  document.getElementById('tls-cert').value = t.cert || '';
  document.getElementById('tls-key').value = t.key || '';
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

document.getElementById('tls-form').addEventListener('submit', e => {
  e.preventDefault();
  post('/api/security', {
    tls_cert: document.getElementById('tls-cert').value,
    tls_key: document.getElementById('tls-key').value,
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

document.getElementById('hosts-form').addEventListener('submit', e => {
  e.preventDefault();
  post('/api/security', {
    extra_hosts: document.getElementById('extra-hosts').value
  }).then(r => {
    toast(r.ok ? (r.data.message || 'Saved.') : r.data.error, r.ok ? 'good' : 'bad');
    if (r.ok) { paint(r.data.state); paintHosts(r.data.state); paintTls(r.data.state); }
  }).catch(() => {});
});

document.getElementById('password-form').addEventListener('submit', e => {
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

document.getElementById('remote').addEventListener('change', e => {
  post('/api/security', { remote_access: e.target.checked }).then(r => {
    toast(r.ok ? r.data.message : r.data.error, r.ok ? 'good' : 'bad');
    if (r.ok) { paint(r.data.state); paintHosts(r.data.state); paintTls(r.data.state); }
    else e.target.checked = !e.target.checked;
  }).catch(() => {});
});

api('/api/security').then(d => { paint(d); paintHosts(d); paintTls(d); }).catch(() => {});
