/* The log viewer. Asks for whatever was appended since the last byte offset
   rather than re-fetching the file: a day's bot log runs to a few hundred
   kilobytes and pulling all of it every two seconds would be silly.

   An offset of -1 means "give me a tail", which is what a first load and a tab
   switch both want. The server also answers with reset:true when the file has
   been replaced — every run gets its own log file, so that happens on every
   restart — and the view is rebuilt rather than appended to. */

let stream = 'athena';
let offset = -1;

function atBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
}

function pull() {
  const el = document.getElementById('log');
  const stick = document.getElementById('follow').checked && atBottom(el);
  return api(`/api/logs?stream=${encodeURIComponent(stream)}&offset=${offset}`)
    .then(d => {
      if (d.reset) el.innerHTML = d.html;
      else if (d.html) el.insertAdjacentHTML('beforeend', d.html);
      offset = d.offset;
      document.getElementById('log-note').textContent =
        d.note || (d.name ? `${d.name} — ${offset.toLocaleString()} bytes` : '');
      if (stick || d.reset) el.scrollTop = el.scrollHeight;
    }).catch(() => {});
}

function tabs(entries) {
  document.getElementById('log-tabs').innerHTML = entries.map(e =>
    `<button data-key="${e.key}" class="${e.key === stream ? 'on' : ''}"
       ${e.path ? '' : 'disabled'} title="${escapeHtml(e.why)}">
       ${escapeHtml(e.title)}</button>`).join('');
}

document.getElementById('log-tabs').addEventListener('click', e => {
  const btn = e.target.closest('button[data-key]');
  if (!btn || btn.disabled || btn.dataset.key === stream) return;
  stream = btn.dataset.key;
  offset = -1;
  document.querySelectorAll('#log-tabs button').forEach(b =>
    b.classList.toggle('on', b.dataset.key === stream));
  pull();
});

api('/api/logstreams')
  .then(d => { tabs(d.streams); pull(); })
  .catch(() => pull());

setInterval(pull, 2000);
