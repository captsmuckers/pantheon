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

/* The three kinds, in the words that describe what they DO. Qwen's own names
   are actively misleading here — "Base" is the one that clones and
   "CustomVoice" is Qwen's own nine voices, which reads as the opposite — so
   the repo path is never shown to anyone. */
const MODES = [
  ['mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit',
   'Use one of Qwen\u2019s 9 ready-made voices',
   'Voices built into the model. Nothing to upload. Only Ryan and Aiden are natively English.'],
  ['mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit',
   'Describe the voice you want, in words',
   'No recording needed \u2014 write what she should sound like. This is what Athena uses by default.'],
  ['mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit',
   'Clone a voice from a recording',
   'Uses your saved voices, or a clip you upload. This is the only kind that uses them.'],
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
  if (!wrap) return;               // the library page has no bench
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
      needs to load it first — about 30 seconds. This loads into the lab's own
      service; Athena keeps talking throughout.
      <button type="button" id="lab-load" class="ghost">Load it
      for testing</button></p>`;
  }

  if (mode === 'customvoice') {
    html += `<div class="field"><label for="lab-voice">Voice</label>
      <select id="lab-voice"></select></div>` + `      <div class="field bake">
        <label>Keep this voice</label>
        <div class="ref-upload">
          <label class="ref-name">Name it
            <input type="text" class="bake-name" placeholder="Athena" maxlength="48"></label>
          <button type="button" class="ghost bake-go">Save this voice to the library</button>
          <span class="try-note bake-note"></span>
        </div>
        <p class="help">Speaks a fixed passage in this voice and stages the
           audio as a library clip, with an exact transcript. From then on it
           behaves like any recorded voice: usable from <code>/tts</code>, and
           clonable without this checkpoint loaded.</p>
      </div>`;
  } else if (mode === 'voicedesign') {
    html += `<div class="field"><label for="lab-design">Describe the voice</label>
      <textarea id="lab-design" rows="3"
        placeholder="a woman in her thirties with a low, dry, aristocratic English voice, bored and faintly contemptuous"
      >${escapeHtml(LIVE.voice_design || '')}</textarea>
      <p class="help">Age, accent, pitch and manner all work. Press Test, adjust
         the wording, test again — it costs nothing and needs no restart.</p></div>` + `      <div class="field bake">
        <label>Keep this voice</label>
        <div class="ref-upload">
          <label class="ref-name">Name it
            <input type="text" class="bake-name" placeholder="Athena" maxlength="48"></label>
          <button type="button" class="ghost bake-go">Save this voice to the library</button>
          <span class="try-note bake-note"></span>
        </div>
        <p class="help">Speaks a fixed passage in this voice and stages the
           audio as a library clip, with an exact transcript. From then on it
           behaves like any recorded voice: usable from <code>/tts</code>, and
           clonable without this checkpoint loaded.</p>
      </div>`;
  } else if (mode === 'base') {
    html += `<div class="field"><label for="lab-saved">Saved voices</label>
      <select id="lab-saved"><option value="">— loading —</option></select>
      <p class="help">Every clip anyone has uploaded. Picking one loads it and
         the words spoken in it together — they are stored as a pair, so an
         old voice never gets a newer voice's transcript.</p></div>
      <div class="field"><label>Or add a new one</label>
      <div class="ref-upload">
        <label class="ref-name">1. Name it
          <input type="text" id="lab-label" placeholder="Athena" maxlength="48"></label>
        <label class="ref-start">start at
          <input type="number" id="lab-start" value="0" min="0" step="1"> s</label>
        <label class="ghost file-btn">2. Upload a sample file
          <input type="file" id="lab-file" accept="audio/*,video/*" hidden></label>
        <span class="try-note" id="lab-upnote"></span>
      </div>
      <p class="help">Up to 90 seconds is kept, levelled and transcribed for
         you. One speaker, no music, in the tone you want back. Use
         <em>start at</em> to skip an intro.</p>
      <input type="hidden" id="lab-ref" value="${escapeHtml(LIVE.voice_ref || '')}">
      <span id="lab-refactions"></span>
      <div class="field" id="lab-reftext-field">
        <label for="lab-reftext">3. Check what it says</label>
        <audio id="lab-refaudio" controls preload="none" hidden></audio>
        <textarea id="lab-reftext" rows="3">${escapeHtml(LIVE.voice_ref_text || '')}</textarea>
        <p class="help"><strong>This one field decides whether the clone sounds
           like the person.</strong> When it matches the recording, the model
           copies the recording itself and keeps rasp, accent and grain. When
           it is blank or wrong, the clip is reduced to a 1024-number average
           that keeps pitch and little else — the voice comes back smooth and
           generic, and nothing reports an error. It is filled in
           automatically and is often imperfect: read it against the clip and
           fix the words and punctuation before you go on.</p>
      </div>
      <div id="lab-pending" hidden>
        <p class="help warn">Now go to <strong>Try it</strong> below, type
           anything you want to hear, and press <strong>Test</strong>. Come
           back here and press <strong>5. Save to library</strong> to keep it,
           or upload a different take. Nothing reaches the library until you
           save, so bad reads and failed clones do not pile up.</p>
        <button type="button" id="lab-save" class="primary">5. Save to library</button>
        <button type="button" id="lab-discard" class="ghost">Discard</button>
      </div>
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
let PENDING = '';        // an uploaded candidate awaiting Save, if any
let PENDING_TEXT = '';   // and what is said in it

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
  const ra = document.getElementById('lab-refaudio');
  if (pick) {
    ref.value = pick.path;
    txt.value = pick.transcript || '';
    if (ra) { ra.src = '/api/tts/clip?name=' + encodeURIComponent(pick.file);
              ra.hidden = false; }
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
  const bake = e.target.closest('.bake-go');
  if (bake) {
    const wrap = bake.closest('.bake');
    const note = wrap.querySelector('.bake-note');
    const label = wrap.querySelector('.bake-name').value.trim();
    if (!label) { note.className = 'try-note bad';
                  note.textContent = 'Give it a name first.'; return; }
    const model = document.getElementById('lab-controls').dataset.model;
    const md = modeOf(model);
    bake.disabled = true;
    note.className = 'try-note';
    note.textContent = 'Speaking the passage in this voice — about 30 seconds…';
    post('/api/tts/voice-refs', { action: 'bake', label,
          instruct: md === 'voicedesign'
            ? (document.getElementById('lab-design') || {}).value || '' : '',
          voice: md === 'customvoice'
            ? (document.getElementById('lab-voice') || {}).value || '' : '' })
      .then(r => {
        bake.disabled = false;
        if (!r || !r.ok || (r.data && r.data.ok === false)) {
          note.className = 'try-note bad';
          note.textContent = (r && r.data && r.data.error) || 'Could not save it.';
          return;
        }
        note.textContent = `Saved as "${r.data.name}" (${r.data.seconds}s). `
                         + 'Use it in Discord with /tts ' + r.data.name
                         + ' — once a cloning checkpoint is loaded.';
        loadLibrary(); loadSaved();
      });
    return;
  }
  const b = e.target.closest('#lab-save, #lab-discard');
  if (b) {
    const note = document.getElementById('lab-upnote');
    const box = document.getElementById('lab-pending');
    if (b.id === 'lab-discard') {
      post('/api/tts/voice-refs', { action: 'discard', pending: PENDING })
        .then(() => {
          PENDING=''; PENDING_TEXT=''; box.hidden = true;
          /* The reference fields still pointed at the file just deleted, so
             the next Test asked for a clip that no longer existed. */
          const ref = document.getElementById('lab-ref');
          const txt = document.getElementById('lab-reftext');
          const ra  = document.getElementById('lab-refaudio');
          if (ref) ref.value = '';
          if (txt) txt.value = '';
          if (ra) { ra.removeAttribute('src'); ra.hidden = true; }
          note.textContent = 'Discarded. Nothing was saved.';
          loadSaved();
        });
      return;
    }
    const label = (document.getElementById('lab-label') || {}).value.trim();
    if (!label) {
      note.className = 'try-note bad';
      note.textContent = 'Give it a name first.';
      return;
    }
    b.disabled = true;
    /* The BOX, not PENDING_TEXT. Test read the field and Save read the raw
       Whisper output, so correcting a transcript produced a good preview and
       then silently saved the uncorrected text — the saved voice never
       matched the one that was auditioned. */
    const said = (document.getElementById('lab-reftext') || {}).value || '';
    post('/api/tts/voice-refs', { action: 'commit', pending: PENDING,
                                  label, transcript: said })
      .then(r => {
        b.disabled = false;
        if (!r.ok) { note.className='try-note bad';
                     note.textContent=(r.data&&r.data.error)||'Could not save.'; return; }
        PENDING=''; box.hidden = true;
        note.className = 'try-note';
        note.textContent = `Saved as "${r.data.name}". Athena is unchanged `
                         + 'until you press Apply.';
        loadSaved().then(() => {
          const sel = document.getElementById('lab-saved');
          if (sel) { sel.value = r.data.path; syncSaved(); }
        });
      });
    return;
  }
  const b2 = e.target.closest('#lab-transcribe');
  if (!b2) return;
  const sel = document.getElementById('lab-saved');
  const pick = SAVED.find(v => v.path === sel.value);
  if (!pick) return;
  b2.disabled = true; b2.textContent = 'Listening…';
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
function waitReady(seconds, endpoint) {
  const deadline = Date.now() + seconds * 1000;
  const url = endpoint || '/api/tts/health';
  const tick = () => fetch(url, { headers: { Accept: 'application/json' } })
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
  }).catch(() => {}).then(() => api('/api/voices/health').catch(() => null))
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
    fetch('/api/voices/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Pantheon-CSRF': '1' },
      /* The reference goes with the request. It used to be left out, so Test
         in cloning mode played whatever clip the service had loaded at
         startup — upload a new voice, press Test, hear the old one, with
         nothing to suggest the upload had not taken. */
      body: JSON.stringify({ voice: p.TTS_VOICE || '', lang: 'auto',
                             instruct: p.TTS_VOICE_DESIGN || '',
                             ref_audio: p.TTS_VOICE_REF || '',
                             ref_text: p.TTS_VOICE_REF_TEXT || '',
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

  if (btn.id === 'lab-load') {
    const note = document.getElementById('lab-note');
    const model = document.getElementById('lab-controls').dataset.model;
    const clones = modeOf(model) === 'base';
    btn.disabled = true;
    note.className = 'try-note';
    note.textContent = 'Loading those weights into the lab — about 30 seconds…';
    post('/api/settings', { values: { VOICES_MODEL: model } }).then(r => {
      if (!r.ok) {
        note.className = 'try-note bad';
        note.textContent = (r.data && r.data.error) || 'Could not load that.';
        btn.disabled = false;
        return;
      }
      return post('/api/service', { service: 'voices', action: 'restart' })
        .then(() => waitReady(120, '/api/voices/health'))
        .then(ok => {
          btn.disabled = false;
          if (!ok) {
            note.className = 'try-note bad';
            note.textContent = 'The lab service did not come back. Check its log.';
            return;
          }
          note.className = clones ? 'try-note' : 'try-note bad';
          note.textContent = clones
            ? 'Loaded. Test away — Athena is untouched.'
            : 'Loaded. Athena is untouched, but /tts in Discord cannot clone '
              + 'saved voices until a cloning checkpoint is loaded again.';
          return refreshLive().then(controls);
        });
    });
    return;
  }

  if (btn.id === 'lab-apply') {
    const applying = true;
    const note = document.getElementById('lab-apply-note');
    btn.disabled = true;
    note.className = 'try-note';
    note.textContent = 'Saving…';
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
               'X-Voice-Label': (document.getElementById('lab-label') || {}).value || '',
               'X-Voice-Start': String(start) },
    body: file
  }).then(r => r.json()).then(d => {
    if (!d.ok) {
      note.className = 'try-note bad';
      note.textContent = d.error || 'Upload failed.';
      return;
    }
    /* A candidate, not a library entry. It is wired into the ref fields so
       Test plays THIS upload, but nothing is written until Save. */
    PENDING = d.pending;
    PENDING_TEXT = d.transcript || '';
    document.getElementById('lab-ref').value = d.path;
    /* Carried so Test uses the SAME mechanism the saved voice will: with a
       transcript, Qwen clones in-context and keeps voice texture; without, it
       reduces the clip to a speaker embedding and smooths rasp away. Testing
       one and saving the other would be a preview that lies. */
    document.getElementById('lab-reftext').value = PENDING_TEXT;
    const ra = document.getElementById('lab-refaudio');
    if (ra) { ra.src = '/api/tts/clip?name=' + encodeURIComponent(PENDING);
              ra.hidden = false; }
    const box = document.getElementById('lab-pending');
    if (box) box.hidden = false;
    note.className = 'try-note';
    note.textContent = `${d.seconds}s ready. ${d.note || ''} Press Test to hear it.`;
  }).catch(() => {
    note.className = 'try-note bad';
    note.textContent = 'Upload failed.';
  }).finally(() => { input.value = ''; });
});

if (document.getElementById('lab-controls')) refreshLive().then(controls);

/* ---- The library -------------------------------------------------------
   Every clip on the machine, listed so it can be removed. Saved voices and
   unsaved candidates are the same kind of thing at different stages, so they
   share a renderer and differ only in which button they get.

   Delete is administrators only, and the server enforces that independently.
   Hiding the button here is courtesy, not security: a General User who forges
   the request still gets a 403 from _voice_refs. */
const IS_ADMIN = (document.body.dataset.role || '') === 'admin';

function libRow(v, pending) {
  const when = pending
    ? `uploaded ${v.age_hours}h ago`
    : (v.added ? escapeHtml(v.added) : '') +
      (v.added_by ? ` · by ${escapeHtml(v.added_by)}` : '');
  const said = v.transcript
    ? `<span class="lib-said">“${escapeHtml(v.transcript.slice(0, 80))}${
         v.transcript.length > 80 ? '…' : ''}”</span>`
    : '<span class="lib-said warn">no transcript — this clip clones as a '
      + 'speaker average and loses rasp and accent</span>';
  /* The transcript is editable in place. It is the field that decides clone
     quality, and Whisper gets it wrong often enough that a library you cannot
     correct is a library of quietly mediocre voices. */
  const edit = `<button type="button" class="ghost lib-edit"
      data-file="${escapeHtml(v.file)}">Edit transcript</button>`;
  const player = `<audio class="lib-audio" controls preload="none"
      src="/api/tts/clip?name=${encodeURIComponent(v.file)}"></audio>`;
  const form = `<div class="lib-edit-box" hidden>
      <textarea rows="3" class="lib-text">${escapeHtml(v.transcript || '')}</textarea>
      <p class="help">Play the clip above and correct this to match it,
         punctuation included. It is what the model aligns the recording
         against, so it decides how closely a clone keeps rasp and accent.
         Takes effect the next time this voice is used — by <code>/tts</code>
         or in the lab; it does not change anything already generated.</p>
      <button type="button" class="lib-save-text"
              data-file="${escapeHtml(v.file)}">Save transcript</button>
      <button type="button" class="ghost lib-cancel">Cancel</button>
      <span class="try-note lib-note"></span>
    </div>`;
  const keep = `<div class="lib-edit-box" hidden>
      <input type="text" class="lib-name" placeholder="Name this voice">
      <textarea rows="3" class="lib-text"
        placeholder="What the recording says — accuracy here decides whether the clone keeps its texture">${escapeHtml(v.transcript || '')}</textarea>
      <button type="button" class="lib-keep" data-file="${escapeHtml(v.file)}">Save to library</button>
      <button type="button" class="ghost lib-cancel">Cancel</button>
    </div>`;
  const act = pending
    ? `<button type="button" class="ghost lib-edit" data-file="${escapeHtml(v.file)}">Keep this</button>
       <button type="button" class="ghost lib-discard" data-file="${escapeHtml(v.file)}">Discard</button>`
    : (IS_ADMIN
        ? `<button type="button" class="ghost lib-delete" data-file="${escapeHtml(v.file)}" data-label="${escapeHtml(v.label)}">Delete</button>`
        : '');
  return `<li class="lib-row">
    <div class="lib-main">
      <strong>${escapeHtml(v.label)}</strong>
      <span class="lib-meta">${v.seconds}s${when ? ' · ' + when : ''}</span>
      ${said}
      ${player}
      ${pending ? keep : form}
    </div>
    <div class="lib-act">${pending ? '' : edit}${act}</div>
  </li>`;
}

function loadLibrary() {
  const box = document.getElementById('lab-library-body');
  if (!box) return Promise.resolve();
  return api('/api/tts/voice-refs').then(d => {
    const saved = d.voices || [], waiting = d.unsaved || [];
    let html = '';
    html += saved.length
      ? `<ul class="lib">${saved.map(v => libRow(v, false)).join('')}</ul>`
      : '<p class="sub">No saved voices yet.</p>';
    if (waiting.length) {
      html += `<h4>Uploaded but never saved (${waiting.length})</h4>
        <p class="sub">Candidates from an upload that was not named and saved.
           They are not usable as voices and are cleared automatically after
           six hours, but only when something else is uploaded.</p>
        <ul class="lib">${waiting.map(v => libRow(v, true)).join('')}</ul>`;
    }
    box.innerHTML = html;
  }).catch(() => {
    box.innerHTML = '<p class="sub bad">Could not read the library.</p>';
  });
}

document.addEventListener('click', e => {
  const ed = e.target.closest('.lib-edit');
  if (ed) {
    const box = ed.closest('.lib-row').querySelector('.lib-edit-box');
    box.hidden = !box.hidden;
    if (!box.hidden) box.querySelector('.lib-text').focus();
    return;
  }
  const cancel = e.target.closest('.lib-cancel');
  if (cancel) { cancel.closest('.lib-edit-box').hidden = true; return; }
  const keepBtn = e.target.closest('.lib-keep');
  if (keepBtn) {
    const box = keepBtn.closest('.lib-edit-box');
    const label = box.querySelector('.lib-name').value.trim();
    if (!label) { toast('Give it a name first.'); return; }
    keepBtn.disabled = true; keepBtn.textContent = 'Saving…';
    post('/api/tts/voice-refs', { action: 'commit',
          pending: keepBtn.dataset.file, label,
          transcript: box.querySelector('.lib-text').value })
      .then(r => {
        if (!r || !r.ok || (r.data && r.data.ok === false)) {
          keepBtn.disabled = false; keepBtn.textContent = 'Save to library';
          toast((r && r.data && r.data.error) || 'Could not save it.');
          return;
        }
        toast(`Saved as "${r.data.name}".`);
        return loadLibrary().then(loadSaved);
      });
    return;
  }
  const sv = e.target.closest('.lib-save-text');
  if (sv) {
    const box = sv.closest('.lib-edit-box');
    sv.disabled = true; sv.textContent = 'Saving…';
    post('/api/tts/voice-refs', { action: 'set-transcript',
          name: sv.dataset.file,
          transcript: box.querySelector('.lib-text').value })
      .then(r => {
        if (!r || !r.ok || (r.data && r.data.ok === false)) {
          sv.disabled = false; sv.textContent = 'Save transcript';
          toast((r && r.data && r.data.error) || 'Could not save it.');
          return;
        }
        const n = box.querySelector('.lib-note');
        if (n) { n.className = 'try-note'; n.textContent = 'Saved.'; }
        toast('Transcript saved.');
        /* Re-render after a beat so "Saved." is actually seen: rebuilding the
           list immediately replaces the row, and the confirmation with it. */
        return new Promise(r => setTimeout(r, 900))
          .then(() => loadLibrary()).then(loadSaved);
      });
    return;
  }
  const del = e.target.closest('.lib-delete');
  const dis = e.target.closest('.lib-discard');
  const btn = del || dis;
  if (!btn) return;
  const file = btn.dataset.file;
  if (del && !confirm(`Delete "${btn.dataset.label}" permanently?\n\n`
                    + 'The clip and its transcript are removed. Anything using '
                    + 'it falls back to a default voice.')) return;
  btn.disabled = true;
  btn.textContent = del ? 'Deleting…' : 'Discarding…';
  post('/api/tts/voice-refs', del ? { action: 'delete', name: file }
                                  : { action: 'discard', pending: file })
    .then(r => {
      if (!r || !r.ok || (r.data && r.data.ok === false)) {
        btn.disabled = false;
        btn.textContent = del ? 'Delete' : 'Discard';
        toast((r.data && r.data.error) || 'Could not remove it.');
        return;
      }
      /* Both lists can change: deleting a saved voice may be the one the
         picker had selected, so rebuild that too. */
      return loadLibrary().then(loadSaved);
    });
});

loadLibrary();
