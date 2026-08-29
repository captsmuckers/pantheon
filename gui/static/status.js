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

/* A meter reads faster than a number when you are checking "is anything on
   fire". The number stays for when you actually want the value. */
function meter(label, pct, detail, extra) {
  const known = pct !== null && pct !== undefined;
  const level = !known ? '' : pct >= 90 ? ' hot' : pct >= 70 ? ' warm' : '';
  return `<div class="gauge">
    <div class="gauge-top">
      <span class="gauge-label">${escapeHtml(label)}</span>
      <span class="gauge-value${level}">${known ? pct + '%' : '—'}</span>
    </div>
    <div class="gauge-track"><div class="gauge-fill${level}"
         style="width:${known ? Math.min(100, pct) : 0}%"></div></div>
    <div class="gauge-detail">${detail ? escapeHtml(detail) : ''}${
      extra ? ` <span class="dim">${escapeHtml(extra)}</span>` : ''}</div>
  </div>`;
}

function paintSystem(sys) {
  const box = document.getElementById('sysmon');
  if (!box || !sys) return;
  const c = sys.cpu || {}, m = sys.memory || {}, g = sys.gpu || {}, t = sys.temperatures || {};

  const cpuDetail = c.cores ? `${c.cores} cores` : '';
  const cpuExtra = (c.user !== undefined && c.user !== null)
    ? `${c.user}% user · ${c.system}% sys` +
      (c.load ? ` · load ${c.load[0]}` : '') : (c.note || '');
  const memDetail = (m.used_gb !== null && m.used_gb !== undefined)
    ? `${m.used_gb} of ${m.total_gb} GB` : '';
  const memExtra = (m.swap_used_mb ? `swap ${m.swap_used_mb} MB` : '') ;

  let html = meter('CPU', c.percent, cpuDetail, cpuExtra)
           + meter('Memory', m.percent, memDetail, memExtra)
           + meter('GPU', g.percent, '', g.note || '');

  /* Temperatures are absent rather than zero, and say why. A gauge reading 0°
     would be a lie; an empty one with no explanation would just look broken. */
  if (!t.available) {
    html += `<div class="gauge gauge-wide">
      <div class="gauge-top">
        <span class="gauge-label">Temperature</span>
        <span class="gauge-value dim">unavailable</span>
      </div>
      <div class="gauge-detail">${escapeHtml(t.reason || '')}</div>
    </div>`;
  }
  box.innerHTML = html;
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
    paintSystem(d.system);
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

/* ---- updates ------------------------------------------------------------
   Deliberately manual. Checking is a click and applying is another, because
   this updates code that is currently running somebody's film, on a machine
   they may not be sitting at. */

function updateBody(d) {
  if (!d.ok) {
    return `<p class="sub">${escapeHtml(d.message)}</p>` +
      (d.hint ? `<pre class="cmd">${escapeHtml(d.hint)}</pre>` : '');
  }
  const at = `<p class="meta">On <b>${escapeHtml(d.branch)}</b> at
      <code>${escapeHtml(d.current)}</code> — ${escapeHtml(d.current_subject)}
      <span class="dim">(${escapeHtml(d.current_date)})</span></p>`;

  if (d.blocked) {
    return at + `<p class="note">${escapeHtml(d.blocked)}</p>` +
      (d.dirty && d.dirty.length
        ? `<pre class="cmd">${d.dirty.map(escapeHtml).join('\n')}</pre>` : '') +
      `<div class="actions"><button id="update-check">Check again</button></div>`;
  }
  if (!d.behind) {
    return at + `<p class="sub">Up to date${d.checked ? ' as of just now' : ''}.</p>
      <div class="actions"><button id="update-check">Check for updates</button></div>`;
  }

  const list = (d.commits || []).map(c =>
    `<div class="cmt"><code>${escapeHtml(c.hash)}</code>
       <span class="dim">${escapeHtml(c.date)}</span>
       <span>${escapeHtml(c.subject)}</span></div>`).join('');
  const deps = (d.dependencies || []).length
    ? `<p class="note">This update changes ${
        d.dependencies.map(x => escapeHtml(x.what)).join(' and ')
      }, which will be reinstalled as part of it.</p>` : '';

  return at + `<p><b>${d.behind} update${d.behind === 1 ? '' : 's'} available.</b></p>
    <div class="commits">${list}</div>${deps}
    <div class="actions">
      <button class="primary" id="update-apply">Update now</button>
      <button id="update-check">Check again</button>
    </div>`;
}

function paintUpdate(d) {
  const box = document.getElementById('update-body');
  if (box) box.innerHTML = updateBody(d);
}

function loadUpdate(check) {
  const box = document.getElementById('update-body');
  if (check && box) box.innerHTML = '<p class="loading">Contacting the repository…</p>';
  const req = check
    ? post('/api/update/check', {}).then(r => ({ ...r.data, checked: true }))
    : api('/api/update/status');
  return req.then(paintUpdate).catch(() => {});
}

document.addEventListener('click', e => {
  if (e.target.id === 'update-check') { loadUpdate(true); return; }
  if (e.target.id !== 'update-apply') return;

  e.target.disabled = true;
  const out = document.getElementById('update-output');
  out.hidden = false;
  out.textContent = '';
  post('/api/update/apply', {}).then(r => {
    if (!r.ok) { toast(r.data.message || 'Could not update.', 'bad'); loadUpdate(false); return; }
    const restarts = r.data.restarts || [];
    let since = 0;
    const poll = setInterval(() => {
      api(`/api/setup/job?id=${encodeURIComponent(r.data.job)}&since=${since}`)
        .then(j => {
          if (!j.ok) { clearInterval(poll); return; }
          if (j.lines.length) {
            out.textContent += j.lines.join('\n') + '\n';
            out.scrollTop = out.scrollHeight;
          }
          since = j.next;
          if (!j.done) return;
          clearInterval(poll);
          if (j.rc === 0) {
            /* The panel is never restarted for you: it is the process serving
               this page, and pulling it out from under itself is
               indistinguishable from a crash. */
            const say = restarts.filter(x => x !== 'panel');
            toast('Updated.' + (say.length
              ? ` Restart ${say.join(' and ')} to apply.` : ''), 'good');
            if (restarts.includes('panel')) {
              out.textContent += '\nThe control panel itself changed. Restart it ' +
                'from the terminal (Ctrl-C, then scripts/start-gui.sh) to pick ' +
                'up the new version.\n';
            }
          } else {
            toast('Update failed — see the output.', 'bad');
          }
          loadUpdate(false);
          refresh();
        }).catch(() => {});
    }, 1000);
  }).catch(() => {});
});

loadUpdate(false);
