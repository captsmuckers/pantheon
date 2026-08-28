document.getElementById('login-form').addEventListener('submit', e => {
  e.preventDefault();
  const err = document.getElementById('login-error');
  err.hidden = true;
  post('/api/login', { password: document.getElementById('password').value })
    .then(r => {
      if (r.ok) { location.href = '/'; return; }
      err.textContent = r.data.error || 'Wrong password.';
      err.hidden = false;
    }).catch(() => {});
});
