/* The settings editor, built entirely from schema.py.

   Nothing about any individual setting is written here: the field types, the
   bounds, the help text, the sections and which ones are advanced all arrive
   from /api/settings. Adding a setting to the schema makes it appear on this
   page with no change to this file, which is the entire point of having a
   schema.

   Secrets are the exception worth reading carefully. The server never sends a
   token value, so the input starts empty for a token that is already set. An
   empty token field on save therefore means "leave it alone", and clearing one
   is a separate, explicit checkbox. Without that distinction, opening the page
   and pressing Save would wipe every credential. */

let FIELDS = [];
let dirty = new Set();

function widget(f) {
  const id = 'f-' + f.name;
  const v = f.value == null ? '' : String(f.value);

  if (f.kind === 'secret') {
    const set = f.set;
    return `<div class="reveal">
        <input type="password" id="${id}" data-name="${f.name}"
               placeholder="${set ? 'unchanged — type to replace' : 'not set'}"
               autocomplete="off">
        <button type="button" class="eye" data-for="${id}">show</button>
      </div>
      <div class="secret-actions">
        ${set ? `<span class="secret-set">stored, ending ${escapeHtml(f.hint)}</span>` : ''}
        ${set && !f.required
            ? `<label class="inline"><input type="checkbox" data-clear="${f.name}"> Clear it</label>`
            : ''}
      </div>`;
  }
  if (f.kind === 'bool') {
    const on = ['1', 'true', 'yes', 'on'].includes(v.toLowerCase());
    return `<label class="inline"><input type="checkbox" id="${id}"
              data-name="${f.name}" ${on ? 'checked' : ''}> enabled</label>`;
  }
  if (f.kind === 'choice') {
    const opts = f.choices.map(c =>
      `<option value="${escapeHtml(c)}" ${c === v ? 'selected' : ''}>${escapeHtml(c || '(blank)')}</option>`
    ).join('');
    const known = f.choices.includes(v);
    return `<select id="${id}" data-name="${f.name}">${opts}
      ${known ? '' : `<option value="${escapeHtml(v)}" selected>${escapeHtml(v)} (not a listed choice)</option>`}
      </select>`;
  }
  if (f.kind === 'text') {
    return `<textarea id="${id}" data-name="${f.name}">${escapeHtml(v)}</textarea>`;
  }
  if (f.kind === 'int' || f.kind === 'float') {
    const step = f.kind === 'float' ? 'any' : '1';
    const lo = f.lo == null ? '' : `min="${f.lo}"`;
    const hi = f.hi == null ? '' : `max="${f.hi}"`;
    return `<input type="number" step="${step}" ${lo} ${hi} id="${id}"
              data-name="${f.name}" value="${escapeHtml(v)}">`;
  }
  return `<input type="text" id="${id}" data-name="${f.name}"
            value="${escapeHtml(v)}" autocomplete="off" spellcheck="false">`;
}

function fieldBlock(f) {
  /* Only the exception is labelled. Almost everything restarts the bot, so
     tagging all 84 of them says nothing and makes the page hard to scan; the
     three that bounce the speech server instead are worth pointing out. */
  const tag = f.restart === 'tts'
    ? '<span class="restart-tag">restarts speech</span>' : '';
  return `<div class="field ${f.advanced ? 'adv' : ''}" id="wrap-${f.name}">
      <label for="f-${f.name}">${escapeHtml(f.name)}${f.required ? '<span class="req">*</span>' : ''}${tag}</label>
      ${widget(f)}
      ${f.help ? `<p class="help">${escapeHtml(f.help)}</p>` : ''}
      <p class="err" hidden></p>
    </div>`;
}

function render(data) {
  FIELDS = data.fields;
  document.getElementById('env-path').textContent = data.env_path;

  const bySection = new Map();
  for (const f of FIELDS) {
    if (!bySection.has(f.section)) bySection.set(f.section, []);
    bySection.get(f.section).push(f);
  }

  let html = '';
  for (const [section, fields] of bySection) {
    const adv = fields.filter(f => f.advanced).length;
    html += `<div class="section-head"><h2>${escapeHtml(section)}</h2>
      <span class="count">${fields.length} setting${fields.length === 1 ? '' : 's'}${
        adv ? `, ${adv} advanced` : ''}</span></div>
      <div class="panel">${fields.map(fieldBlock).join('')}</div>`;
  }
  document.getElementById('sections').innerHTML = html;
}

function currentValue(f) {
  const el = document.getElementById('f-' + f.name);
  if (!el) return null;
  if (f.kind === 'bool') return el.checked ? 'true' : 'false';
  return el.value;
}

function originalValue(f) {
  if (f.kind === 'bool') {
    return ['1', 'true', 'yes', 'on'].includes(String(f.value).toLowerCase())
      ? 'true' : 'false';
  }
  return f.value == null ? '' : String(f.value);
}

function recompute() {
  dirty = new Set();
  for (const f of FIELDS) {
    if (f.kind === 'secret') {
      const el = document.getElementById('f-' + f.name);
      const clear = document.querySelector(`[data-clear="${f.name}"]`);
      if ((el && el.value) || (clear && clear.checked)) dirty.add(f.name);
      continue;
    }
    if (currentValue(f) !== originalValue(f)) dirty.add(f.name);
  }
  const bar = document.getElementById('savebar');
  bar.hidden = dirty.size === 0;
  if (dirty.size) {
    const restarts = new Set();
    for (const name of dirty) {
      const f = FIELDS.find(x => x.name === name);
      restarts.add(f && f.restart === 'tts' ? 'Speech' : 'Athena');
    }
    document.getElementById('save-summary').textContent =
      `${dirty.size} change${dirty.size === 1 ? '' : 's'} — will need ` +
      `${[...restarts].join(' and ')} restarted`;
  }
}

document.getElementById('sections').addEventListener('input', recompute);
document.getElementById('sections').addEventListener('change', recompute);

document.getElementById('show-advanced').addEventListener('change', e => {
  document.body.classList.toggle('show-adv', e.target.checked);
});

document.getElementById('revert').addEventListener('click', () => load());

document.getElementById('settings-form').addEventListener('submit', e => {
  e.preventDefault();
  const values = {}, clear = [];
  for (const f of FIELDS) {
    if (!dirty.has(f.name)) continue;
    const box = document.querySelector(`[data-clear="${f.name}"]`);
    if (f.kind === 'secret' && box && box.checked) { clear.push(f.name); continue; }
    values[f.name] = currentValue(f);
  }

  document.querySelectorAll('.field.bad').forEach(el => {
    el.classList.remove('bad');
    el.querySelector('.err').hidden = true;
  });

  post('/api/settings', { values, clear }).then(r => {
    if (r.ok) {
      toast(r.data.message, 'good');
      load();
      return;
    }
    /* A rejected save changes nothing on disk, so every bad field is marked
       and the form is left exactly as the user typed it. */
    const fields = r.data.fields || {};
    for (const [name, problem] of Object.entries(fields)) {
      const wrap = document.getElementById('wrap-' + name);
      if (!wrap) continue;
      wrap.classList.add('bad');
      const err = wrap.querySelector('.err');
      err.textContent = problem;
      err.hidden = false;
      if (FIELDS.find(f => f.name === name)?.advanced) {
        document.getElementById('show-advanced').checked = true;
        document.body.classList.add('show-adv');
      }
    }
    const first = document.querySelector('.field.bad');
    if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
    toast(r.data.error || 'Nothing was saved.', 'bad');
  }).catch(() => {});
});

function load() {
  return api('/api/settings').then(d => { render(d); recompute(); });
}
load();
