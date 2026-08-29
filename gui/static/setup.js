/* The first-run wizard.

   Every step reports one of three states. `ok` needs nothing. `todo` blocks the
   bot from starting. `optional` is a feature that is simply off — speech and
   voice input are genuinely optional and the page should not nag about them.

   Some steps this page can do itself; some it cannot, and says so with the
   exact command instead of a button that could not succeed. Installing a
   system audio driver needs a password, and a macOS permission grant needs a
   human to answer a dialog. */

let POLL = null;

function stepCard(s) {
  const icon = { ok: '✓', todo: '•', optional: '○' }[s.state];
  const canFix = s.fix && s.state !== 'ok';
  return `<section class="panel step ${s.state}">
    <div class="step-head">
      <span class="step-icon">${icon}</span>
      <h3>${escapeHtml(s.title)}</h3>
      <span class="step-state">${s.state === 'ok' ? 'done'
        : s.state === 'optional' ? 'not set up' : 'needed'}</span>
    </div>
    <p class="sub">${escapeHtml(s.why)}</p>
    <p class="step-detail">${escapeHtml(s.detail)}</p>
    ${s.state !== 'ok' && s.manual
      ? `<pre class="cmd">${escapeHtml(s.manual)}</pre>` : ''}
    ${s.settings && s.settings.length
      ? `<p><a href="/settings#Discord">Fill these in on the Settings page →</a></p>` : ''}
    <div class="step-actions">
      ${canFix ? `<button type="button" class="primary do-fix"
           data-action="${escapeHtml(s.fix)}">Do this for me</button>` : ''}
    </div>
  </section>`;
}

function paint(d) {
  const box = document.getElementById('setup-summary');
  box.innerHTML = d.ready
    ? `<p class="good-line">Everything required is in place.
         <a href="/">Go to the status page →</a></p>`
    : `<p><b>${d.remaining} thing${d.remaining === 1 ? '' : 's'} left</b> before
         the bot can start. Optional steps are marked as such.</p>`;
  document.getElementById('setup-steps').innerHTML = d.steps.map(stepCard).join('');
}

function refresh() {
  return api('/api/setup/status').then(paint).catch(() => {});
}

document.addEventListener('click', e => {
  const btn = e.target.closest('.do-fix');
  if (!btn) return;
  const action = btn.dataset.action;
  document.querySelectorAll('.do-fix').forEach(b => b.disabled = true);

  post('/api/setup/run', { action }).then(r => {
    if (!r.ok) {
      toast(r.data.message || 'Could not start that.', 'bad');
      document.querySelectorAll('.do-fix').forEach(b => b.disabled = false);
      return;
    }
    if (r.data.done) {           // instant actions, like copying .env
      toast(r.data.message, 'good');
      refresh().then(() => document.querySelectorAll('.do-fix')
        .forEach(b => b.disabled = false));
      return;
    }
    watch(r.data.job, r.data.title);
  }).catch(() => {});
});

/* Output is streamed rather than shown at the end: a pip install of PyTorch
   with no output for four minutes is indistinguishable from a hang, and the
   natural response to that is to press the button again. */
function watch(jobId, title) {
  const panel = document.getElementById('job-panel');
  const out = document.getElementById('job-output');
  const status = document.getElementById('job-status');
  panel.hidden = false;
  document.getElementById('job-title').textContent = title;
  out.textContent = '';
  status.textContent = 'Working…';
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  let since = 0;
  clearInterval(POLL);
  POLL = setInterval(() => {
    api(`/api/setup/job?id=${encodeURIComponent(jobId)}&since=${since}`)
      .then(j => {
        if (!j.ok) { clearInterval(POLL); status.textContent = j.message; return; }
        if (j.lines.length) {
          out.textContent += j.lines.join('\n') + '\n';
          out.scrollTop = out.scrollHeight;
        }
        since = j.next;
        status.textContent = j.done
          ? (j.rc === 0 ? `Finished in ${j.elapsed}s.` : `Failed (exit ${j.rc}).`)
          : `Working… ${j.elapsed}s`;
        if (j.done) {
          clearInterval(POLL);
          toast(j.rc === 0 ? `${title} — done.` : `${title} — failed.`,
                j.rc === 0 ? 'good' : 'bad');
          refresh().then(() => document.querySelectorAll('.do-fix')
            .forEach(b => b.disabled = false));
        }
      }).catch(() => {});
  }, 1000);
}

refresh();
