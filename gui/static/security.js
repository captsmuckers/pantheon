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
    if (r.ok) paint(r.data.state);
    else e.target.checked = !e.target.checked;
  }).catch(() => {});
});

api('/api/security').then(paint).catch(() => {});
