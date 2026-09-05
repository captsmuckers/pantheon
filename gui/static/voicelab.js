/* The voice lab.
   A bench, not a settings form. Everything here is local until Apply, which
   is the only thing that writes .env or restarts anything.

   What is instant and what is not is decided by the engine, not by us: the
   voice name and the description ride on every synthesize call, so trying
   those costs nothing, while the MODE is the checkpoint and changing it means
   loading different weights. The page says which is which rather than letting
   a thirty second pause look like a hang. */

let LIVE = {};        // what the running service is actually doing
let FIELDS = {};      // the schema entries we are allowed to see

const MODES = [
  ['mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit', 'Pick a ready-made voice',
   'Nine voices built into the model. Only Ryan and Aiden are natively English.'],
  ['mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit', 'Describe a voice',
   'Write what she should sound like. The wording matters more than you expect.'],
  ['mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit', 'Copy a voice from a recording',
   'Upload a clip. Only the first 6 seconds decide who it sounds like.'],
];

function modeOf(model) {
  const m = (model || '').toLowerCase();
  if (m.includes('customvoice')) return 'customvoice';
  if (m.includes('voicedesign')) return 'voicedesign';
  if (m.includes('base')) return 'base';
  return '';
}

function paintLive() {
  const box = document.getElementById('lab-live');
  if (!box) return;
  const eng = LIVE.engine || '?';
  const label = { customvoice: 'a ready-made voice', voicedesign: 'a described voice',
                  base: 'a copied voice' }[LIVE.qwen_mode] || '';
  box.innerHTML = eng === 'qwen'
    ? `Loaded now: <strong>Qwen</strong>, using ${label}. Voices and descriptions
       test instantly; choosing a different <em>kind</em> of voice below has to
       load different weights, which takes about 30 seconds.`
    : `Loaded now: <strong>${escapeHtml(eng)}</strong>. Applying a Qwen voice will
       switch the engine, which restarts the speech service.`;
}

function controls() {
  const wrap = document.getElementById('lab-controls');
  const chosen = wrap.dataset.model || LIVE.qwen_model || MODES[0][0];
  const mode = modeOf(chosen);
  const needsLoad = mode !== LIVE.qwen_mode || LIVE.engine !== 'qwen';

  let html = '<h3>What kind of voice</h3><div class="lab-modes">';
  for (const [model, title, blurb] of MODES) {
    html += `<label class="lab-mode${model === chosen ? ' on' : ''}">
      <input type="radio" name="lab-mode" value="${escapeHtml(model)}"
             ${model === chosen ? 'checked' : ''}>
      <strong>${escapeHtml(title)}</strong>
      <span class="sub">${escapeHtml(blurb)}</span></label>`;
  }
  html += '</div>';

  if (needsLoad) {
    html += `<p class="help warn">This is not the kind currently loaded, so Test
      needs to load it first — about 30 seconds, and it interrupts her speech
      while it happens. <button type="button" id="lab-load" class="ghost">Load it
      for testing</button></p>`;
  }

  if (mode === 'customvoice') {
    html += `<div class="field"><label for="lab-voice">Voice</label>
      <select id="lab-voice"></select></div>`;
  } else if (mode === 'voicedesign') {
    html += `<div class="field"><label for="lab-design">Describe the voice</label>
      <textarea id="lab-design" rows="3"
        placeholder="a woman in her thirties with a low, dry, aristocratic English voice, bored and faintly contemptuous"
      >${escapeHtml(LIVE.voice_design || '')}</textarea>
      <p class="help">Age, accent, pitch and manner all work. Press Test, adjust
         the wording, test again — it costs nothing and needs no restart.</p></div>`;
  } else if (mode === 'base') {
    html += `<div class="field"><label for="lab-saved">Saved voices</label>
      <select id="lab-saved"><option value="">— loading —</option></select>
      <p class="help">Every clip anyone has uploaded. Picking one loads it and
         the words spoken in it together — they are stored as a pair, so an
         old voice never gets a newer voice's transcript.</p></div>
      <div class="field"><label>Or add a new one</label>
      <div class="ref-upload">
        <label class="ghost file-btn">Upload a clip
          <input type="file" id="lab-file" accept="audio/*,video/*" hidden></label>
        <label class="ref-start">start at
          <input type="number" id="lab-start" value="0" min="0" step="1"> s</label>
        <span class="try-note" id="lab-upnote"></span>
      </div>
      <p class="help">Trimmed to 10 seconds, levelled and transcribed for you.
         One speaker, no music, in the tone you want back.</p>
      <input type="hidden" id="lab-ref" value="${escapeHtml(LIVE.voice_ref || '')}">
      <span id="lab-refactions"></span>
      <input type="hidden" id="lab-reftext" value="${escapeHtml(LIVE.voice_ref_text || '')}">
      <p class="help" id="lab-refnow">${LIVE.voice_ref
        ? 'Currently using: <code>' + escapeHtml(LIVE.voice_ref) + '</code>'
        : '<strong>No recording set.</strong> Until you upload one and press Apply, '
          + 'this mode speaks in a default voice that resembles nothing.'}</p></div>`;
  }
  wrap.innerHTML = html;
  wrap.dataset.model = chosen;
  if (mode === 'customvoice') loadVoices();
  if (mode === 'base') loadSaved();
}

function loadVoices() {
  api('/api/tts/voices').then(d => {
    const sel = document.getElementById('lab-voice');
    if (!sel) return;
    const list = d.voices || [];
    if (!list.length) {
      /* value="" is load-bearing. An <option> with no value attribute takes
         its TEXT as its value, so this placeholder was submitted verbatim as
         TTS_VOICE, written to .env, and passed to the speech service as
         --voice - which argparse rejected, crash-looping it at boot. */
      sel.innerHTML = '<option value="">— load this kind first to list its voices —</option>';
      return;
    }
    sel.innerHTML = list.map(v =>
      `<option value="${escapeHtml(v.id)}"${v.id === LIVE.voice ? ' selected' : ''}>
         ${escapeHtml(v.id)} — ${escapeHtml(v.note || v.lang_name || '')}</option>`).join('');
  }).catch(() => {});
}


/* The saved-voice library.
   Clips live in tts/voices with their transcript in a .json beside them, so a
   voice is one thing rather than two settings that can drift apart. Picking
   one fills BOTH fields; there is no way to select a clip and inherit the
   wrong words. */
let SAVED = [];

function loadSaved() {
  return api('/api/tts/voice-refs').then(d => {
    SAVED = d.voices || [];
    const sel = document.getElementById('lab-saved');
    if (!sel) return;
    const cur = (document.getElementById('lab-ref') || {}).value || '';
    sel.innerHTML = '<option value="">— none —</option>' + SAVED.map(v =>
      `<option value="${escapeHtml(v.path)}"${v.path === cur ? ' selected' : ''}>
         ${escapeHtml(v.label)} · ${v.seconds}s${v.added_by ? ' · added by ' + escapeHtml(v.added_by) : ''}
         ${v.needs_transcript ? ' · no transcript yet' : ''}</option>`).join('');
    syncSaved();
  }).catch(() => {});
}

function syncSaved() {
  const sel = document.getElementById('lab-saved');
  const ref = document.getElementById('lab-ref');
  const txt = document.getElementById('lab-reftext');
  const now = document.getElementById('lab-refnow');
  if (!sel || !ref) return;
  const pick = SAVED.find(v => v.path === sel.value);
  if (pick) {
    ref.value = pick.path;
    txt.value = pick.transcript || '';
    if (now) {
      now.innerHTML = pick.transcript
        ? 'Using <code>' + escapeHtml(pick.label) + '</code>. It says: “'
          + escapeHtml(pick.transcript.slice(0, 90)) + '…”'
        : '<strong>No transcript for this clip.</strong> It will still work, '
          + 'but copies the voice less well. '
          + '<button type="button" class="ghost" id="lab-transcribe">Transcribe it</button>';
    }
  } else if (now) {
    now.innerHTML = '<strong>No recording chosen.</strong> Pick a saved voice or '
                  + 'upload one — until then this mode speaks in a default voice.';
  }
}

document.addEventListener('change', e => {
  if (e.target && e.target.id === 'lab-saved') syncSaved();
});

document.addEventListener('click', e => {
  const b = e.target.closest('#lab-transcribe');
  if (!b) return;
  const sel = document.getElementById('lab-saved');
  const pick = SAVED.find(v => v.path === sel.value);
  if (!pick) return;
  b.disabled = true; b.textContent = 'Listening…';
  post('/api/tts/voice-refs', { action: 'transcribe', name: pick.name + '.wav' })
    .then(() => loadSaved());
});

/* What the lab is currently proposing, as settings names. */
function proposed() {
  const model = document.getElementById('lab-controls').dataset.model;
  const out = { TTS_ENGINE: 'qwen', TTS_QWEN_MODEL: model };
  const g = id => (document.getElementById(id) || {}).value || '';
  const mode = modeOf(model);
  /* Only ever propose a value that exists. A blank one means the controls are
     not ready yet — sending it would clear a working setting. */
  if (mode === 'customvoice' && g('lab-voice')) out.TTS_VOICE = g('lab-voice');
  if (mode === 'voicedesign' && g('lab-design')) out.TTS_VOICE_DESIGN = g('lab-design');
  if (mode === 'base') {
    /* Same guard as the other two, and it was missing here: an empty value is
       written as an empty setting, not skipped, so applying before the upload
       finished silently cleared the reference. Base then generates with no
       clip at all — which produces a perfectly good voice that sounds nothing
       like the recording, and reports no error at any layer. */
    if (g('lab-ref')) out.TTS_VOICE_REF = g('lab-ref');
    if (g('lab-reftext')) out.TTS_VOICE_REF_TEXT = g('lab-reftext');
  }
  return out;
}

/* Poll until the speech service says it is ready, because a restart returns
   as soon as launchd accepts it and the model load takes ~30s after that.
   Reporting success early is what made "loading" look like it did nothing,
   and then made Test hit a service that was not up. */
function waitReady(seconds) {
  const deadline = Date.now() + seconds * 1000;
  const tick = () => fetch('/api/tts/health', { headers: { Accept: 'application/json' } })
    .then(r => r.json())
    .then(h => {
      if (h && h.ready) return true;
      if (Date.now() > deadline) return false;
      return new Promise(res => setTimeout(res, 2000)).then(tick);
    })
    .catch(() => (Date.now() > deadline
      ? false : new Promise(res => setTimeout(res, 2000)).then(tick)));
  return tick();
}

function refreshLive() {
  return api('/api/status').then(d => {
    LIVE = (d.probes && d.probes.tts && d.probes.tts.detail) || LIVE;
  }).catch(() => {}).then(() => api('/api/tts/health').catch(() => null))
    .then(h => { if (h) LIVE = Object.assign({}, LIVE, h); paintLive(); });
}

document.addEventListener('change', e => {
  if (e.target && e.target.name === 'lab-mode') {
    document.getElementById('lab-controls').dataset.model = e.target.value;
    controls();
  }
});

document.addEventListener('click', e => {
  const btn = e.target.closest('#lab-test, #lab-apply, #lab-load');
  if (!btn) return;

  if (btn.id === 'lab-test') {
    const note = document.getElementById('lab-note');
    const audio = document.getElementById('lab-audio');
    const p = proposed();
    btn.disabled = true;
    note.className = 'try-note';
    note.textContent = 'Speaking…';
    fetch('/api/tts/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Pantheon-CSRF': '1' },
      body: JSON.stringify({ voice: p.TTS_VOICE || '', lang: 'auto',
                             instruct: p.TTS_VOICE_DESIGN || '',
                             text: document.getElementById('lab-text').value })
    }).then(async r => {
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        note.className = 'try-note bad';
        note.textContent = d.error || 'Could not speak that.';
        return;
      }
      const url = URL.createObjectURL(await r.blob());
      if (audio.src) URL.revokeObjectURL(audio.src);
      audio.src = url; audio.hidden = false; audio.play().catch(() => {});
      note.textContent = 'Played here only — not in Discord.';
    }).finally(() => { btn.disabled = false; });
    return;
  }

  if (btn.id === 'lab-apply' || btn.id === 'lab-load') {
    const applying = btn.id === 'lab-apply';
    const note = document.getElementById(applying ? 'lab-apply-note' : 'lab-note');
    btn.disabled = true;
    note.className = 'try-note';
    note.textContent = applying ? 'Saving…' : 'Loading those weights…';
    post('/api/settings', { values: proposed() }).then(r => {
      if (!r.ok) {
        note.className = 'try-note bad';
        note.textContent = (r.data && r.data.error) || 'Could not save.';
        btn.disabled = false;
        return;
      }
      note.textContent = 'Restarting the speech service — this loads the model, '
                       + 'about 30 seconds…';
      return post('/api/service', { service: 'tts', action: 'restart' })
        .then(() => waitReady(90))
        .then(ok => {
          if (!ok) {
            note.className = 'try-note bad';
            note.textContent = 'The speech service did not come back. Check its log.';
            btn.disabled = false;
            return;
          }
          return post('/api/service', { service: 'bot', action: 'restart' })
            .then(() => {
              note.textContent = applying
                ? 'Applied. She is using this voice in Discord now.'
                : 'Loaded. Test away.';
              btn.disabled = false;
              return refreshLive().then(controls);
            });
        });
    });
  }
});

document.addEventListener('change', e => {
  const input = e.target.closest('#lab-file');
  if (!input || !input.files || !input.files.length) return;
  const file = input.files[0];
  const note = document.getElementById('lab-upnote');
  const start = (document.getElementById('lab-start') || {}).value || 0;
  note.className = 'try-note';
  note.textContent = `Uploading ${file.name}, then transcribing…`;
  fetch('/api/tts/voice-ref', {
    method: 'POST',
    headers: { 'X-Pantheon-CSRF': '1', 'Content-Type': 'application/octet-stream',
               'X-Voice-Filename': file.name.replace(/[^\x20-\x7e]/g, '_'),
               'X-Voice-Start': String(start) },
    body: file
  }).then(r => r.json()).then(d => {
    if (!d.ok) {
      note.className = 'try-note bad';
      note.textContent = d.error || 'Upload failed.';
      return;
    }
    SAVED = d.voices || SAVED;
    note.textContent = `${d.seconds}s saved as "${d.name}". ${d.note || ''} `
                     + 'It is in Saved voices now. Press Test to hear it.';
    loadSaved().then(() => {
      const sel = document.getElementById('lab-saved');
      if (sel) { sel.value = d.path; syncSaved(); }
    });
  }).catch(() => {
    note.className = 'try-note bad';
    note.textContent = 'Upload failed.';
  }).finally(() => { input.value = ''; });
});

refreshLive().then(controls);
