/* The status page. Polls, because the alternative is a websocket and there is
   nothing here that changes fast enough to justify one. */

const NAMES = { bot: 'bot', tts: 'speech server' };

function serviceCard(key, s) {
  const foreign = s.foreign || [];
  let dot, word;
  if (s.running) { dot = 'up'; word = 'Running'; }
  else if (foreign.length) { dot = 'other'; word = 'Not this one'; }
  else { dot = 'down'; word = 'Stopped'; }

  let meta = '';
  if (s.running) {
    meta = `<p class="meta">pid <b>${s.pids.join(', ')}</b> · up <b>${escapeHtml(s.uptime)}</b>`
         + (s.supervised ? ' · <b>supervised by launchd</b>, so it restarts on crash'
                         : ' · started by hand, so a crash stays down') + '</p>';
  } else if (s.last_exit !== null && s.last_exit !== undefined) {
    meta = `<p class="meta">last exit code <b>${s.last_exit}</b>`
         + (s.last_exit === 15 ? ' (terminated)' : '') + '</p>';
  }

  /* The case that matters on a machine with two checkouts: a bot IS running,
     just not the one this panel controls. Saying "Stopped" would invite a
     Start that puts two bots in the same Discord channel. */
  let note = '';
  if (!s.running && foreign.length) {
    const where = foreign.map(f => escapeHtml(f.where)).join(', ');
    note = `<p class="note">A ${NAMES[key]} is running from <code>${where}</code>,
            which this panel does not control. Starting one here would put two
            of them in the same channel.</p>`;
  } else if (s.launchd && s.launchd.installed && !s.launchd.ours) {
    note = `<p class="note">A launchd agent named <code>${escapeHtml(s.launchd.label || '')}</code>
            exists but points at another checkout, so start and stop here use
            the scripts directly.</p>`;
  }

  let health = '';
  if (key === 'tts' && s.health) {
    health = s.health.error
      ? `<p class="meta">health check failed — <b>${escapeHtml(s.health.error)}</b></p>`
      : `<p class="meta">${s.health.ready ? 'model loaded' : '<b>still loading</b>'}
         · voice <b>${escapeHtml(s.health.voice || '?')}</b>
         · <b>${escapeHtml(s.health.engine || '?')}</b> on
         <b>${escapeHtml(s.health.device || '?')}</b></p>`;
  }

  return `<div class="svc" data-key="${key}">
    <div class="svc-top">
      <h3><span class="dot ${dot}"></span>${escapeHtml(s.title)}</h3>
      <span class="state-word">${word}</span>
    </div>
    ${meta}${health}${note}
    <div class="actions">
      <button data-act="start" ${s.running ? 'disabled' : ''}>Start</button>
      <button data-act="stop" ${s.running ? '' : 'disabled'}>Stop</button>
      <button data-act="restart" ${s.running ? '' : 'disabled'}>Restart</button>
    </div>
  </div>`;
}

function probeRow(p) {
  return `<div class="probe">
    <span class="tag">${escapeHtml(p.name)}</span>
    <span class="${p.ok ? 'ok' : 'no'}">${p.ok ? '✓' : '✗'} ${escapeHtml(p.detail)}</span>
    <span class="why">${escapeHtml(p.why)}</span>
  </div>`;
}

let busy = false;
function refresh() {
  if (busy) return Promise.resolve();
  return api('/api/status').then(d => {
    document.getElementById('services').innerHTML =
      ['bot', 'tts'].map(k => serviceCard(k, d.services[k])).join('');
    document.getElementById('probes').innerHTML = d.probes.map(probeRow).join('');
  }).catch(() => {});
}

document.getElementById('services').addEventListener('click', e => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const card = btn.closest('.svc');
  const action = btn.dataset.act, key = card.dataset.key;

  /* The panel refuses to be the reason two bots end up in one Discord channel.
     The card already explains that another checkout is running; this is the
     step that makes ignoring it deliberate rather than a single click. */
  if (action === 'start' && card.querySelector('.note')) {
    const where = card.querySelector('.note code');
    if (where && !confirm(
        `A ${NAMES[key]} is already running from ${where.textContent.trim()}.\n\n` +
        `Starting another means two of them answering the same channel. Continue?`)) {
      return;
    }
  }

  busy = true;
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  btn.textContent = action === 'stop' ? 'Stopping…' : 'Starting…';

  post('/api/service', { service: key, action }).then(r => {
    const m = r.data.message || '';
    const notes = (r.data.notes || []).join(' ');
    toast(r.ok ? [m, notes].filter(Boolean).join(' — ')
               : (r.data.detail || r.data.message || 'Failed'),
          r.ok ? 'good' : 'bad');
  }).catch(() => {}).finally(() => {
    busy = false;
    // launchd's kickstart returns before the process is up; give it a beat.
    setTimeout(refresh, 1200);
  });
});

refresh();
setInterval(refresh, 5000);
