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
    /* Only offer an unlisted value when there actually is one. An empty value
       is "not set", which the default now covers, not a mystery choice. */
    const unlisted = v && !f.choices.includes(v);
    return `<select id="${id}" data-name="${f.name}">${opts}
      ${unlisted ? `<option value="${escapeHtml(v)}" selected>${escapeHtml(v)} (not a listed choice)</option>` : ''}
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
      ${extras(f)}
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
    html += `<section class="secpage" data-section="${escapeHtml(section)}">
      <div class="section-head"><h2>${escapeHtml(section)}</h2>
        <span class="count">${fields.length} setting${fields.length === 1 ? '' : 's'}${
          adv ? `, ${adv} advanced` : ''}</span></div>
      <div class="panel">${fields.map(fieldBlock).join('')}</div>
    </section>`;
  }
  document.getElementById('sections').innerHTML = html;

  SECTIONS = [...bySection.keys()];
  document.getElementById('secnav').innerHTML = SECTIONS.map(sec =>
    `<button type="button" data-section="${escapeHtml(sec)}">
       <span>${escapeHtml(sec)}</span>
       <span class="badge" hidden></span>
     </button>`).join('');

  showSection(sectionFromHash() || SECTIONS[0]);
}

let SECTIONS = [];

function sectionFromHash() {
  const want = decodeURIComponent((location.hash || '').replace(/^#/, ''));
  return SECTIONS.includes(want) ? want : '';
}

/* Only one section on screen at a time. The page was 10,600 pixels tall with
   advanced settings shown, which made finding anything a scroll. */
function showSection(section) {
  document.querySelectorAll('.secpage').forEach(el => {
    el.hidden = el.dataset.section !== section;
  });
  document.querySelectorAll('#secnav button').forEach(b => {
    b.classList.toggle('on', b.dataset.section === section);
  });
  if (decodeURIComponent((location.hash || '').slice(1)) !== section) {
    history.replaceState(null, '', '#' + encodeURIComponent(section));
  }
  window.scrollTo(0, 0);
}

document.getElementById('secnav').addEventListener('click', e => {
  const btn = e.target.closest('button[data-section]');
  if (btn) showSection(btn.dataset.section);
});
window.addEventListener('hashchange', () => {
  const s = sectionFromHash();
  if (s) showSection(s);
});

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
      restarts.add(f && f.restart === 'tts' ? 'Speech' : BOT_NAME);
    }
    const where = new Set();
    for (const name of dirty) {
      const f = FIELDS.find(x => x.name === name);
      if (f) where.add(f.section);
    }
    const secs = [...where];
    document.getElementById('save-summary').textContent =
      `${dirty.size} change${dirty.size === 1 ? '' : 's'} in ${secs.join(', ')}` +
      ` — will need ${[...restarts].join(' and ')} restarted`;
  }
  paintBadges();
}

/* A count on each section in the nav, because unsaved changes are otherwise
   invisible the moment you switch away from them. */
function paintBadges() {
  const per = new Map();
  for (const name of dirty) {
    const f = FIELDS.find(x => x.name === name);
    if (f) per.set(f.section, (per.get(f.section) || 0) + 1);
  }
  document.querySelectorAll('#secnav button').forEach(b => {
    const n = per.get(b.dataset.section) || 0;
    const badge = b.querySelector('.badge');
    badge.textContent = n;
    badge.hidden = n === 0;
    b.classList.toggle('dirty', n > 0);
  });
}

document.getElementById('sections').addEventListener('input', recompute);
document.getElementById('sections').addEventListener('change', recompute);

document.getElementById('show-advanced').addEventListener('change', e => {
  document.body.classList.toggle('show-adv', e.target.checked);
});

document.getElementById('setting-filter').addEventListener('input', e => {
  const q = e.target.value.trim().toLowerCase();
  document.body.classList.toggle('filtering', !!q);
  if (!q) {
    document.querySelectorAll('.field').forEach(f => f.classList.remove('hit'));
    showSection(sectionFromHash() || SECTIONS[0]);
    return;
  }
  /* Searching means searching everything, so every section is shown and only
     matching fields within them. Name and help text both, because nobody
     remembers that the wake word setting is called VOICE_WAKE_WORDS. */
  let total = 0;
  document.querySelectorAll('.secpage').forEach(page => {
    let hits = 0;
    page.querySelectorAll('.field').forEach(field => {
      const f = FIELDS.find(x => 'wrap-' + x.name === field.id);
      const hay = f ? (f.name + ' ' + (f.help || '')).toLowerCase() : '';
      const hit = hay.includes(q);
      field.classList.toggle('hit', hit);
      if (hit) hits++;
    });
    page.hidden = hits === 0;
    total += hits;
  });
  document.getElementById('save-summary');
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

/* Two settings get more than a plain input, because both are things you want
   to try rather than guess at. The voice gets a Test button that speaks with
   whatever is currently TYPED — not what is saved — so a voice can be judged
   before committing to it. The language gets a live report of which
   phonemisers are actually installed. */
function extras(f) {
  if (f.name === 'TTS_VOICE') {
    return `<div class="try">
        <button type="button" id="try-voice">▶ Test this voice</button>
        <button type="button" id="browse-voices" class="ghost">Browse voices</button>
        <span class="try-note" id="try-note"></span>
        <audio id="try-audio" controls hidden></audio>
      </div>
      <div id="voice-list" class="voice-list" hidden></div>`;
  }
  if (f.name === 'TTS_LANG_CODE') {
    return `<div id="lang-state" class="lang-state"></div>`;
  }
  return '';
}

function currentVoiceAndLang() {
  const v = document.getElementById('f-TTS_VOICE');
  const l = document.getElementById('f-TTS_LANG_CODE');
  return { voice: v ? v.value.trim() : '', lang: l ? l.value : 'auto' };
}

/* Which languages the serving process can phonemise. Asked of the speech
   server rather than hardcoded here: the answer depends on what is installed
   in its virtualenv, and a copy kept in this file would be wrong the moment
   somebody pressed Install. */
let BOT_NAME = 'the bot';
let LANGS = {};
let INSTALL_NOTES = {};

function paintLanguages(d) {
  const box = document.getElementById('lang-state');
  const sel = document.getElementById('f-TTS_LANG_CODE');
  if (!box) return;

  if (!d.ok) {
    box.innerHTML = `<p class="note-sm">${escapeHtml(d.message || 'Speech service unreachable.')}</p>`;
    return;
  }
  LANGS = d.languages || {};
  INSTALL_NOTES = d.installable || {};

  /* Annotate the dropdown itself, so an unavailable language is obvious at the
     moment of choosing rather than after saving and restarting. */
  if (sel) {
    for (const opt of sel.options) {
      const info = LANGS[opt.value];
      if (!info) continue;
      opt.textContent = info.available
        ? `${opt.value} — ${info.name}`
        : `${opt.value} — ${info.name} (needs ${info.needs})`;
    }
  }
  renderLangNotice();
}

function renderLangNotice() {
  const box = document.getElementById('lang-state');
  const { lang } = currentVoiceAndLang();
  const info = LANGS[lang];
  if (!box) return;
  if (!info || info.available) { box.innerHTML = ''; return; }
  const extra = INSTALL_NOTES[lang] || '';
  box.innerHTML = `<div class="note-sm warn">
      <b>${escapeHtml(info.name)} needs an extra package.</b>
      Its pronunciation rules are not part of the base install, so the speech
      service would refuse to start with this selected.
      ${extra ? `<br>${escapeHtml(extra)}` : ''}
      <div class="secret-actions">
        <button type="button" class="install-lang" data-lang="${lang}">
          Install ${escapeHtml(info.needs)}</button>
        <span class="install-note"></span>
      </div>
    </div>`;
}

document.addEventListener('change', e => {
  if (e.target.id === 'f-TTS_LANG_CODE') renderLangNotice();
});

document.addEventListener('click', e => {
  const btn = e.target.closest('#try-voice, .install-lang');
  if (!btn) return;

  if (btn.id === 'try-voice') {
    const { voice, lang } = currentVoiceAndLang();
    const note = document.getElementById('try-note');
    const audio = document.getElementById('try-audio');
    btn.disabled = true;
    note.textContent = 'Synthesising…';
    note.className = 'try-note';
    fetch('/api/tts/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Pantheon-CSRF': '1' },
      body: JSON.stringify({ voice, lang })
    }).then(async r => {
      if (r.ok) {
        const url = URL.createObjectURL(await r.blob());
        if (audio.src) URL.revokeObjectURL(audio.src);
        audio.src = url;
        audio.hidden = false;
        audio.play().catch(() => {});
        note.textContent = `${voice || 'default'} · ${lang}`;
        return;
      }
      const d = await r.json().catch(() => ({}));
      note.textContent = d.error || 'Preview failed.';
      note.className = 'try-note bad';
      if (d.needs) renderLangNotice();
    }).catch(() => {
      note.textContent = 'Preview failed.';
      note.className = 'try-note bad';
    }).finally(() => { btn.disabled = false; });
    return;
  }

  /* Installing is slow — a pip download and install — so the button says so
     rather than appearing to hang. */
  const lang = btn.dataset.lang;
  const note = btn.parentElement.querySelector('.install-note');
  btn.disabled = true;
  note.textContent = INSTALL_NOTES[lang]
    ? 'Installing… ' + INSTALL_NOTES[lang]
    : 'Installing… this takes a minute.';
  post('/api/tts/install-language', { lang }).then(r => {
    note.textContent = r.data.message || '';
    toast(r.data.message || 'Done', r.ok ? 'good' : 'bad');
    if (r.ok) loadLanguages();
  }).catch(() => {}).finally(() => { btn.disabled = false; });
});

function loadLanguages() {
  return api('/api/tts/languages').then(paintLanguages).catch(() => {});
}

/* The published voices, listed in the panel because they are genuinely hard to
   find: Kokoro's model page does not enumerate them, and the only real list is
   a VOICES.md inside the repo. Clicking one fills the field but does not save,
   so the natural move is pick, Test, then Save if you like it. */
function paintVoices(d) {
  const box = document.getElementById('voice-list');
  if (!box) return;
  if (!d.count) {
    box.innerHTML = `<p class="note-sm">${escapeHtml(d.message || 'Voice list unavailable.')}
      <a href="${escapeHtml(d.doc)}" target="_blank" rel="noreferrer noopener">Full list on HuggingFace</a></p>`;
    return;
  }
  const groups = new Map();
  for (const v of d.voices) {
    if (!groups.has(v.lang_name)) groups.set(v.lang_name, []);
    groups.get(v.lang_name).push(v);
  }
  let html = `<p class="note-sm">${d.count} published voices. The prefix is the
      language and the sex — <code>bf_</code> is British female. Ones you have
      already used are marked; the rest download on first use.
      <a href="${escapeHtml(d.doc)}" target="_blank" rel="noreferrer noopener">Quality grades</a> ·
      <a href="${escapeHtml(d.samples)}" target="_blank" rel="noreferrer noopener">Audio samples</a></p>`;
  for (const [lang, list] of [...groups].sort()) {
    html += `<div class="vgroup"><h4>${escapeHtml(lang)}</h4><div class="vchips">`
      + list.map(v => `<button type="button" class="vchip${v.downloaded ? ' have' : ''}"
           data-voice="${escapeHtml(v.id)}" title="${v.downloaded ? 'already downloaded' : 'downloads on first use'}"
           >${escapeHtml(v.id)}</button>`).join('')
      + `</div></div>`;
  }
  box.innerHTML = html;
}

function loadVoices() {
  return api('/api/tts/voices').then(paintVoices).catch(() => {});
}

document.addEventListener('click', e => {
  const toggle = e.target.closest('#browse-voices');
  if (toggle) {
    const box = document.getElementById('voice-list');
    box.hidden = !box.hidden;
    toggle.textContent = box.hidden ? 'Browse voices' : 'Hide voices';
    if (!box.hidden && !box.innerHTML) { box.innerHTML = '<p class="loading">Loading…</p>'; loadVoices(); }
    return;
  }
  const chip = e.target.closest('.vchip');
  if (!chip) return;
  const input = document.getElementById('f-TTS_VOICE');
  input.value = chip.dataset.voice;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  document.querySelectorAll('.vchip.on').forEach(c => c.classList.remove('on'));
  chip.classList.add('on');
  document.getElementById('try-voice').click();
});

function load() {
  /* The bot's own name, so the save bar says "Hermes" on an install that
     renamed it rather than always "Athena". */
  return api('/api/status').then(d => {
    BOT_NAME = (d.services && d.services.bot && d.services.bot.title) || 'the bot';
  }).catch(() => {}).then(() => api('/api/settings'))
    .then(d => { render(d); recompute(); })
    .then(loadLanguages);
}
load();
