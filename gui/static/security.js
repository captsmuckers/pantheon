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


/* Restart the panel itself.
   The reply has to be sent BEFORE the process dies, so the server answers
   first and boots itself a moment later; this then polls until the socket
   answers again rather than guessing at a delay. launchd's KeepAlive is what
   actually brings it back. */
on('restart-panel', 'click', () => {
  const note = document.getElementById('restart-note');
  const btn = document.getElementById('restart-panel');
  btn.disabled = true;
  note.className = 'try-note';
  note.textContent = 'Restarting…';
  post('/api/panel/restart', {}).then(() => {
    let tries = 0;
    const probe = () => {
      tries += 1;
      fetch('/api/status', { headers: { 'Accept': 'application/json' } })
        .then(r => {
          if (!r.ok) throw new Error('not yet');
          note.textContent = 'Back up. Reloading…';
          setTimeout(() => location.reload(), 400);
        })
        .catch(() => {
          if (tries > 40) {
            note.className = 'try-note bad';
            note.textContent = 'Did not come back after 20s — check the gui log.';
            btn.disabled = false;
            return;
          }
          setTimeout(probe, 500);
        });
    };
    setTimeout(probe, 1500);
  }).catch(() => {
    /* The connection dropping IS the restart happening, so this is expected
       rather than a failure — fall through to the same polling. */
    setTimeout(() => location.reload(), 4000);
  });
});

/* Accounts.
   The table is rebuilt from the server's answer after every change rather
   than patched in place: the server refuses some edits (removing the last
   administrator, deleting yourself) and the only honest view of who exists is
   the one it just sent back. */
function paintUsers(d) {
  const t = document.getElementById('users-table');
  if (!t) return;
  const me = d.me || '';
  t.innerHTML = '<tr><th>Name</th><th>Role</th><th></th></tr>' +
    (d.users || []).map(u => `<tr>
      <td>${escapeHtml(u.name)}${u.name === me ? ' <span class="you">(you)</span>' : ''}</td>
      <td>${u.role === 'admin' ? 'Administrator' : 'General User'}</td>
      <td>${u.name === me ? ''
        : `<button type="button" class="ghost del-user" data-name="${escapeHtml(u.name)}">Remove</button>`}</td>
    </tr>`).join('');
}

function loadUsers() {
  return api('/api/users').then(paintUsers).catch(() => {});
}

on('user-form', 'submit', e => {
  e.preventDefault();
  const note = document.getElementById('user-note');
  const name = document.getElementById('u-name').value.trim();
  const pass = document.getElementById('u-pass').value;
  const role = document.getElementById('u-role').value;
  note.className = 'try-note';
  note.textContent = 'Saving…';
  post('/api/users', { action: 'save', name, password: pass, role })
    .then(r => {
      if (!r.ok) {
        note.className = 'try-note bad';
        note.textContent = (r.data && r.data.error) || 'Could not save.';
        return;
      }
      document.getElementById('u-pass').value = '';
      note.textContent = `Saved ${name}.`;
      paintUsers({ users: r.data.users, me: (r.data.me || '') });
      loadUsers();
    });
});

document.addEventListener('click', e => {
  const btn = e.target.closest('.del-user');
  if (!btn) return;
  const name = btn.dataset.name;
  if (!confirm(`Remove the account "${name}"? They are signed out immediately.`)) return;
  post('/api/users', { action: 'delete', name }).then(r => {
    const note = document.getElementById('user-note');
    if (!r.ok) {
      note.className = 'try-note bad';
      note.textContent = (r.data && r.data.error) || 'Could not remove.';
      return;
    }
    note.className = 'try-note';
    note.textContent = `Removed ${name}.`;
    loadUsers();
  });
});

loadUsers();
