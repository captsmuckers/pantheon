/* Your own account. The password endpoint takes the name from the session, so
   there is nothing here that can be aimed at somebody else's account. */
api('/api/whoami').then(d => {
  const el = document.getElementById('acct-who');
  if (el && d && d.user) {
    el.textContent = d.user + (d.role === 'admin' ? ' (administrator)' : ' (general user)');
  }
}).catch(() => {});

document.getElementById('pw-save').addEventListener('click', () => {
  const btn = document.getElementById('pw-save');
  const note = document.getElementById('pw-note');
  const cur = document.getElementById('pw-current').value;
  const nw = document.getElementById('pw-new').value;
  const again = document.getElementById('pw-again').value;
  const bad = m => { note.className = 'try-note bad'; note.textContent = m; };
  if (!cur) return bad('Enter your current password.');
  if (nw.length < 8) return bad('The new password needs at least 8 characters.');
  if (nw !== again) return bad('The two new passwords do not match.');
  btn.disabled = true;
  note.className = 'try-note';
  note.textContent = 'Changing…';
  post('/api/password', { current: cur, new: nw }).then(r => {
    btn.disabled = false;
    if (!r || !r.ok || (r.data && r.data.ok === false)) {
      bad((r && r.data && r.data.error) || 'Could not change it.');
      return;
    }
    note.className = 'try-note';
    note.textContent = r.data.message || 'Password changed.';
    ['pw-current', 'pw-new', 'pw-again'].forEach(i => {
      document.getElementById(i).value = '';
    });
  });
});
