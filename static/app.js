const $ = (id) => document.getElementById(id);
const state = { mode: 'text', audioFile: null, recorder: null, chunks: [], recordStartedAt: 0, recordTimer: null, current: null, live: null };

const show = (el, yes = true) => el.classList.toggle('hidden', !yes);
function setError(message) { $('error-box').textContent = message || ''; show($('error-box'), Boolean(message)); }
function setBusy(button, busy, label) {
  if (busy) { button.dataset.oldText = button.innerHTML; button.innerHTML = `<span class="spinner"></span>${label}`; button.disabled = true; }
  else { button.innerHTML = button.dataset.oldText || label; button.disabled = false; }
}
async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

async function refreshHealth() {
  try {
    const health = await apiFetch('/api/health');
    const dot = document.createElement('span'); dot.className = `status-dot ${health.api_key_configured ? 'ok' : ''}`;
    $('provider-status').textContent = health.api_key_configured ? 'Cloud AI connected' : 'API key needed';
    $('provider-status').before(dot);
    const dots = document.querySelectorAll('.top-status .status-dot');
    if (dots.length > 1) dots[0].remove();
    if (!health.api_key_configured) $('provider-status').title = 'Add SARVAM_API_KEY to the .env file and restart the server.';
  } catch { $('provider-status').textContent = 'API status unavailable'; }
}

function switchMode(mode) {
  if (mode !== 'live' && state.live && !state.live.finished && (state.live.active || (state.live.socket && state.live.socket.readyState < WebSocket.CLOSING))) stopLiveTranslation(state.live);
  state.mode = mode;
  document.querySelectorAll('.mode-tab').forEach((tab) => {
    const active = tab.dataset.mode === mode;
    tab.classList.toggle('active', active); tab.setAttribute('aria-selected', String(active));
  });
  show($('text-mode'), mode === 'text'); show($('audio-mode'), mode === 'audio'); show($('live-mode'), mode === 'live');
  show($('translate-row'), mode !== 'live'); setError('');
}
document.querySelectorAll('.mode-tab').forEach((tab) => tab.addEventListener('click', () => switchMode(tab.dataset.mode)));
$('source-text').addEventListener('input', () => $('char-count').textContent = `${$('source-text').value.length.toLocaleString()} / 12,000`);

function setAudioFile(file) {
  if (!file) return;
  if (file.size > 15 * 1024 * 1024) { setError('That file is over 15 MB. Choose a shorter or smaller recording.'); return; }
  state.audioFile = file; $('audio-file').value = '';
  $('selected-audio').textContent = `${file.name || 'Recorded audio'} · ${(file.size / 1024 / 1024).toFixed(2)} MB`;
  show($('selected-audio')); $('transcribe-button').disabled = false; setError('');
}
$('audio-file').addEventListener('change', (e) => setAudioFile(e.target.files?.[0]));
$('dropzone').addEventListener('dragover', (e) => { e.preventDefault(); $('dropzone').classList.add('dragover'); });
$('dropzone').addEventListener('dragleave', () => $('dropzone').classList.remove('dragover'));
$('dropzone').addEventListener('drop', (e) => { e.preventDefault(); $('dropzone').classList.remove('dragover'); setAudioFile(e.dataTransfer.files?.[0]); });

$('record-button').addEventListener('click', async () => {
  const button = $('record-button');
  if (state.recorder && state.recorder.state === 'recording') { state.recorder.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    setError('Audio recording is not supported in this browser. Use Choose file instead.'); return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const options = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? { mimeType: 'audio/webm;codecs=opus' } : {};
    state.recorder = new MediaRecorder(stream, options); state.chunks = [];
    state.recorder.ondataavailable = (event) => { if (event.data.size) state.chunks.push(event.data); };
    state.recorder.onstop = () => {
      // Strip browser-added codec parameters from the MIME metadata before upload.
      // The audio bytes remain WebM/Opus; the provider expects a base MIME such as audio/webm.
      const recordedMime = state.recorder.mimeType || 'audio/webm';
      const mime = recordedMime.split(';', 1)[0].trim().toLowerCase();
      const ext = mime.includes('mp4') ? 'm4a' : mime.includes('ogg') ? 'ogg' : 'webm';
      setAudioFile(new File(state.chunks, `tamil-recording.${ext}`, { type: mime }));
      stream.getTracks().forEach((track) => track.stop());
      button.textContent = '● Record audio'; button.classList.remove('recording'); show($('recording-indicator'), false);
      clearInterval(state.recordTimer);
    };
    state.recorder.start(); state.recordStartedAt = Date.now(); button.textContent = '■ Stop recording'; button.classList.add('recording'); show($('recording-indicator'));
    state.recordTimer = setInterval(() => {
      const seconds = Math.floor((Date.now() - state.recordStartedAt) / 1000);
      $('record-time').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
      if (seconds >= 29 && state.recorder?.state === 'recording') state.recorder.stop();
    }, 250);
    setError('');
  } catch (err) { setError(`Could not access the microphone: ${err.message || 'permission denied'}. You can upload an audio file instead.`); }
});

$('transcribe-button').addEventListener('click', async () => {
  if (!state.audioFile) return;
  const button = $('transcribe-button'); setError(''); setBusy(button, true, 'Transcribing…');
  try {
    const form = new FormData(); form.append('file', state.audioFile, state.audioFile.name || 'recording.webm');
    const result = await apiFetch('/api/transcribe', { method: 'POST', body: form });
    $('transcript-text').value = result.transcript; show($('transcript-wrap'));
    $('transcript-wrap').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) { setError(err.message); }
  finally { setBusy(button, false, 'Transcribe audio'); button.disabled = !state.audioFile; }
});

$('use-transcript-button').addEventListener('click', () => {
  const transcript = $('transcript-text').value.trim();
  if (!transcript) { setError('The transcript is empty. Edit it or transcribe the audio again.'); return; }
  $('source-text').value = transcript; $('char-count').textContent = `${transcript.length.toLocaleString()} / 12,000`;
  switchMode('text'); $('source-text').focus(); setError('');
});

async function doTranslate() {
  const text = state.mode === 'audio' ? $('transcript-text').value.trim() : $('source-text').value.trim();
  if (!text) { setError(state.mode === 'audio' ? 'Transcribe audio first, then review the transcript.' : 'Enter Tamil text or Tanglish first.'); return; }
  const button = $('translate-button'); setError(''); setBusy(button, true, 'Understanding context…');
  try {
    const result = await apiFetch('/api/translate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, context: $('context-text').value, style: $('style-select').value, input_kind: $('input-kind').value })
    });
    state.current = result;
    $('result-source').textContent = result.source_text;
    $('english-output').textContent = result.english; $('hindi-output').textContent = result.hindi;
    $('meaning-output').textContent = result.literal_meaning || 'No separate meaning note was returned.';
    $('notes-output').textContent = result.notes || 'No extra ambiguity noted.';
    $('english-audio').removeAttribute('src'); $('hindi-audio').removeAttribute('src');
    $('english-audio-status').textContent = ''; $('hindi-audio-status').textContent = '';
    show($('results-section')); $('results-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) { setError(err.message); }
  finally { setBusy(button, false, 'Translate'); }
}
$('translate-button').addEventListener('click', doTranslate);

async function speak(which) {
  if (!state.current) return;
  const english = which === 'english';
  const text = english ? state.current.english : state.current.hindi;
  const lang = english ? 'en-IN' : 'hi-IN';
  const audio = $(english ? 'english-audio' : 'hindi-audio');
  const status = $(english ? 'english-audio-status' : 'hindi-audio-status');
  const button = document.querySelector(`[data-speak="${which}"]`);
  if (audio.src && audio.src.startsWith('data:audio')) { audio.classList.remove('hidden'); audio.play(); return; }
  const original = button.innerHTML; button.disabled = true; button.innerHTML = '◌ Generating voice…'; status.textContent = 'Creating audio'; setError('');
  try {
    const result = await apiFetch('/api/speak', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, language: lang }) });
    audio.src = result.audio_data_url; audio.classList.add('hidden'); status.textContent = 'Ready'; await audio.play();
  } catch (err) { status.textContent = ''; setError(err.message); }
  finally { button.disabled = false; button.innerHTML = original; }
}
document.querySelectorAll('[data-speak]').forEach((button) => button.addEventListener('click', () => speak(button.dataset.speak)));
document.querySelectorAll('[data-copy]').forEach((button) => button.addEventListener('click', async () => {
  const key = button.dataset.copy; const text = state.current?.[key]; if (!text) return;
  try { await navigator.clipboard.writeText(text); button.textContent = '✓'; setTimeout(() => button.textContent = '▢', 1200); }
  catch { setError('Clipboard access was blocked by the browser. Select and copy the translation manually.'); }
}));

function feedback(isGood) {
  document.querySelectorAll('.feedback-button').forEach((b) => b.classList.remove('selected'));
  $(isGood ? 'good-feedback' : 'bad-feedback').classList.add('selected');
  $('feedback-status').textContent = isGood ? 'Thanks — noted for this session.' : 'Thanks. Review or edit the source/context and try another style.';
}
$('good-feedback').addEventListener('click', () => feedback(true));
$('bad-feedback').addEventListener('click', () => feedback(false));


// Live microphone translation. The browser sends PCM16/16 kHz chunks to FastAPI;
// the backend proxies them to Sarvam's realtime WebSocket without exposing the key.
function makeLiveSession() {
  return {
    socket: null, stream: null, audioContext: null, source: null, processor: null, silentGain: null,
    active: false, stopping: false, history: [], translationQueue: Promise.resolve(), voiceQueue: Promise.resolve(),
    stopTimer: null, startedAt: 0, finished: false,
    audioMode: 'focus', noiseSuppression: true, echoCancellation: true,
    groupRecorder: null, groupChunks: [], recordedBlob: null, recordedFilename: 'solai-group-session.webm',
    recordingTooLarge: false, recorderError: ''
  };
}

function setLiveStatus(text, connected = false) {
  $('live-status').textContent = text;
  $('live-state-dot').classList.toggle('ok', connected);
}

function formatTime(seconds) {
  const safe = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(safe / 60);
  const wholeSeconds = Math.floor(safe % 60);
  return `${String(minutes).padStart(2, '0')}:${String(wholeSeconds).padStart(2, '0')}`;
}

function updateAudioModeUI() {
  const group = $('live-processing-mode').value === 'group';
  const live = state.live;
  const running = Boolean(live && (live.active || live.stopping));
  $('live-processing-mode').disabled = running;
  $('live-noise-suppression').disabled = running;
  $('live-echo-cancellation').disabled = running;
  $('live-speaker-count').disabled = !group || running;
  $('speaker-count-field').classList.toggle('setting-disabled', !group);
  $('live-audio-note').textContent = group
    ? 'Group Mode keeps a temporary local audio recording. Live captions/translations still arrive in real time; speaker labels are analyzed after you stop and choose Analyze speakers. Music and overlapping voices may still affect accuracy.'
    : 'Focus Mode stays live. Browser noise suppression helps with steady noise, but it cannot reliably remove music or separate overlapping voices.';
  const canAnalyze = Boolean(live && live.audioMode === 'group' && live.recordedBlob && !live.recordingTooLarge);
  $('analyze-speakers-button').disabled = !canAnalyze || $('analyze-speakers-button').dataset.busy === 'true';
  if (!group && !running) $('group-recording-status').textContent = 'Focus Mode is selected. Switch to Group Mode before starting a session to enable speaker analysis.';
  else if (group && !live) $('group-recording-status').textContent = 'Group Mode records a temporary audio clip in this browser. The clip is uploaded only when you explicitly choose Analyze speakers.';
}

function resampleMono(input, inputRate, outputRate) {
  if (inputRate === outputRate) return input;
  const outLength = Math.max(1, Math.floor(input.length * outputRate / inputRate));
  const output = new Float32Array(outLength);
  const ratio = inputRate / outputRate;
  for (let i = 0; i < outLength; i++) {
    const position = i * ratio;
    const left = Math.floor(position);
    const fraction = position - left;
    const right = Math.min(left + 1, input.length - 1);
    output[i] = input[left] * (1 - fraction) + input[right] * fraction;
  }
  return output;
}

function pcm16Base64(samples) {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < samples.length; i++) {
    const value = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, value < 0 ? value * 32768 : value * 32767, true);
  }
  let binary = '';
  const step = 0x8000;
  for (let start = 0; start < bytes.length; start += step) {
    binary += String.fromCharCode(...bytes.subarray(start, Math.min(start + step, bytes.length)));
  }
  return btoa(binary);
}

async function startLiveAudioGraph(live) {
  let context;
  try { context = new AudioContext({ sampleRate: 16000 }); }
  catch { context = new AudioContext(); }
  live.audioContext = context;
  await context.resume();
  live.source = context.createMediaStreamSource(live.stream);
  live.processor = context.createScriptProcessor(2048, 1, 1);
  live.silentGain = context.createGain();
  live.silentGain.gain.value = 0;
  live.processor.onaudioprocess = (event) => {
    if (!live.active || !live.socket || live.socket.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    const pcm = resampleMono(input, context.sampleRate, 16000);
    if (!pcm.length) return;
    const audio = pcm16Base64(pcm);
    try { live.socket.send(JSON.stringify({ event: 'audio_input', audio })); }
    catch (err) { setError(`Could not stream microphone audio: ${err.message || 'connection error'}`); }
  };
  live.source.connect(live.processor);
  live.processor.connect(live.silentGain);
  live.silentGain.connect(context.destination);
  live.active = true;
  live.startedAt = Date.now();
  $('live-caption-state').textContent = 'Listening · partial captions update as you speak';
  $('live-partial').textContent = 'Listening… speak Tamil naturally. Pause briefly between phrases for translations.';
  $('live-start-button').textContent = '■ Stop live translation';
  $('live-start-button').classList.add('recording');
  setLiveStatus('Listening to Tamil', true);
}

function startGroupRecording(live) {
  if (live.audioMode !== 'group') return;
  if (!window.MediaRecorder) {
    throw new Error('This browser cannot record Group Mode audio for post-session speaker analysis. Focus Mode can still be used.');
  }
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
  const options = {};
  const supported = candidates.find((type) => MediaRecorder.isTypeSupported(type));
  if (supported) options.mimeType = supported;
  const recorder = new MediaRecorder(live.stream, options);
  live.groupRecorder = recorder;
  live.groupChunks = [];
  live.recordedBlob = null;
  live.recordingTooLarge = false;
  live.recorderError = '';
  recorder.ondataavailable = (event) => {
    if (!event.data?.size) return;
    live.groupChunks.push(event.data);
    const size = live.groupChunks.reduce((total, chunk) => total + chunk.size, 0);
    if (size > 15 * 1024 * 1024) {
      live.recordingTooLarge = true;
      $('group-recording-status').textContent = 'Group recording exceeded 15 MB. Live translation continues, but this clip is too large for speaker analysis.';
      $('analyze-speakers-button').disabled = true;
    }
  };
  recorder.onerror = () => {
    live.recorderError = 'The browser recording failed. Live captions may continue, but speaker analysis will not be available.';
    $('group-recording-status').textContent = live.recorderError;
  };
  recorder.onstop = () => {
    if (live.groupChunks.length && !live.recordingTooLarge && !live.recorderError) {
      const rawType = recorder.mimeType || live.groupChunks[0]?.type || 'audio/webm';
      const mimeType = rawType.split(';', 1)[0].trim().toLowerCase() || 'audio/webm';
      const extension = mimeType.includes('mp4') ? 'm4a' : mimeType.includes('ogg') ? 'ogg' : mimeType.includes('wav') ? 'wav' : 'webm';
      live.recordedFilename = `solai-group-session.${extension}`;
      live.recordedBlob = new Blob(live.groupChunks, { type: mimeType });
      $('group-recording-status').textContent = `Group audio ready (${(live.recordedBlob.size / 1024 / 1024).toFixed(2)} MB). Nothing has been uploaded yet.`;
    } else if (!live.recordingTooLarge) {
      $('group-recording-status').textContent = live.recorderError || 'No group audio was captured. Try another session.';
    }
    updateAudioModeUI();
  };
  recorder.start(1000);
  $('group-recording-status').textContent = 'Recording locally while live translation runs. The clip is not uploaded unless you choose Analyze speakers.';
}

async function stopGroupRecording(live) {
  const recorder = live?.groupRecorder;
  if (!recorder || recorder.state === 'inactive') return;
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 2500);
    recorder.addEventListener('stop', () => { clearTimeout(timer); resolve(); }, { once: true });
    try { recorder.stop(); } catch { clearTimeout(timer); resolve(); }
  });
}

async function analyzeGroupSpeakers() {
  const live = state.live;
  if (!live || live.audioMode !== 'group' || !live.recordedBlob || live.recordingTooLarge) return;
  const button = $('analyze-speakers-button');
  button.dataset.busy = 'true';
  button.disabled = true;
  button.textContent = 'Uploading audio…';
  const output = $('speaker-analysis-results');
  output.innerHTML = '';
  output.classList.remove('hidden');
  const status = $('group-recording-status');
  try {
    const form = new FormData();
    form.append('file', new File([live.recordedBlob], live.recordedFilename, { type: live.recordedBlob.type || 'audio/webm' }));
    const requestedSpeakers = $('live-speaker-count').value;
    if (requestedSpeakers !== 'auto') form.append('num_speakers', requestedSpeakers);
    const created = await apiFetch('/api/diarize', { method: 'POST', body: form });
    status.textContent = 'Speaker analysis queued. The live translation transcript remains available above.';
    button.textContent = 'Analyzing speakers…';
    let job = null;
    for (let attempt = 0; attempt < 125; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 2500));
      job = await apiFetch(`/api/diarize/${encodeURIComponent(created.job_id)}`);
      if (job.status === 'completed' || job.status === 'failed') break;
    }
    if (!job || job.status !== 'completed') {
      throw new Error(job?.detail || 'Speaker analysis timed out. Try a shorter recording or inspect the server logs.');
    }
    const segments = Array.isArray(job.result?.segments) ? job.result.segments : [];
    if (!segments.length) throw new Error('No speaker-labelled segments were found in the recording.');
    status.textContent = `Found ${job.result.speaker_count} speaker label(s) across ${segments.length} speech segments. Translating the segments now…`;
    const cards = new Map();
    output.innerHTML = '';
    for (const segment of segments) {
      const card = document.createElement('article'); card.className = 'speaker-segment';
      const head = document.createElement('div'); head.className = 'speaker-segment-head';
      const speaker = document.createElement('strong'); speaker.textContent = segment.speaker || 'Speaker';
      const time = document.createElement('span'); time.textContent = `${formatTime(segment.start_time_seconds)}–${formatTime(segment.end_time_seconds)}`;
      head.append(speaker, time);
      const source = document.createElement('p'); source.className = 'speaker-segment-source'; source.textContent = segment.text || '';
      const translations = document.createElement('div'); translations.className = 'speaker-segment-translations';
      const en = document.createElement('div'); en.className = 'en';
      const enLabel = document.createElement('small'); enLabel.textContent = 'ENGLISH';
      const enText = document.createElement('p'); enText.textContent = 'Translation pending…'; en.append(enLabel, enText);
      const hi = document.createElement('div'); hi.className = 'hi';
      const hiLabel = document.createElement('small'); hiLabel.textContent = 'हिन्दी';
      const hiText = document.createElement('p'); hiText.textContent = 'अनुवाद प्रतीक्षारत…'; hi.append(hiLabel, hiText);
      translations.append(en, hi); card.append(head, source, translations); output.append(card);
      cards.set(String(segment.id), { enText, hiText });
    }
    const context = [
      $('context-text').value.trim(),
      segments.slice(0, 12).map((segment) => `${segment.speaker}: ${segment.text}`).join('\n')
    ].filter(Boolean).join('\n\n').slice(0, 3000);
    for (let i = 0; i < segments.length; i += 8) {
      const batch = segments.slice(i, i + 8);
      const result = await apiFetch('/api/translate-speaker-segments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ segments: batch, context, style: $('style-select').value })
      });
      for (const translated of result.translations || []) {
        const target = cards.get(String(translated.id));
        if (target) { target.enText.textContent = translated.english; target.hiText.textContent = translated.hindi; }
      }
      status.textContent = `Translated ${Math.min(i + batch.length, segments.length)} of ${segments.length} speaker segments…`;
    }
    status.textContent = 'Post-session speaker analysis and translation complete. Speaker labels are estimates; overlapping speech can still be misattributed.';
    button.textContent = 'Analyze speakers again';
  } catch (err) {
    status.textContent = `Speaker analysis failed: ${err.message || 'unknown error'}`;
    button.textContent = 'Retry speaker analysis';
  } finally {
    delete button.dataset.busy;
    updateAudioModeUI();
    if (live.recordedBlob && !live.recordingTooLarge) button.disabled = false;
  }
}

async function releaseLiveAudio(live) {
  live.active = false;
  try { if (live.processor) { live.processor.onaudioprocess = null; live.processor.disconnect(); } } catch {}
  try { if (live.source) live.source.disconnect(); } catch {}
  try { if (live.silentGain) live.silentGain.disconnect(); } catch {}
  if (live.stream) { live.stream.getTracks().forEach((track) => track.stop()); live.stream = null; }
  if (live.audioContext && live.audioContext.state !== 'closed') {
    try { await live.audioContext.close(); } catch {}
  }
}

function finishLiveSession(live, statusText = 'Live translation stopped') {
  if (!live || live.finished) return;
  live.finished = true;
  live.stopping = false;
  if (live.stopTimer) { clearTimeout(live.stopTimer); live.stopTimer = null; }
  stopGroupRecording(live).catch(() => {});
  releaseLiveAudio(live);
  if (live.socket && live.socket.readyState < WebSocket.CLOSING) {
    try { live.socket.close(); } catch {}
  }
  $('live-start-button').disabled = false;
  $('live-start-button').textContent = '● Start live translation';
  $('live-start-button').classList.remove('recording');
  $('live-caption-state').textContent = 'Microphone stopped';
  setLiveStatus(statusText, false);
}

async function startLiveTranslation() {
  if (state.live && (state.live.active || state.live.stopping)) {
    await stopLiveTranslation(state.live);
    return;
  }
  const button = $('live-start-button');
  setError('');
  if (!navigator.mediaDevices?.getUserMedia || !window.WebSocket) {
    setError('This browser does not support live microphone streaming. Try a current version of Chrome or Edge.');
    return;
  }
  const live = makeLiveSession();
  live.audioMode = $('live-processing-mode').value;
  live.noiseSuppression = $('live-noise-suppression').checked;
  live.echoCancellation = $('live-echo-cancellation').checked;
  state.live = live;
  $('live-processing-mode').disabled = true;
  $('live-noise-suppression').disabled = true;
  $('live-echo-cancellation').disabled = true;
  $('live-speaker-count').disabled = true;
  $('speaker-analysis-results').innerHTML = '';
  show($('speaker-analysis-results'), false);
  button.disabled = true;
  button.textContent = 'Allow microphone…';
  setLiveStatus('Requesting microphone permission…');
  try {
    live.stream = await navigator.mediaDevices.getUserMedia({ audio: {
      channelCount: 1,
      echoCancellation: live.echoCancellation,
      noiseSuppression: live.noiseSuppression,
      autoGainControl: true
    } });
    if (live.audioMode === 'group') startGroupRecording(live);
    else $('group-recording-status').textContent = 'Focus Mode is active. Switch to Group Mode before starting a session to enable speaker analysis.';
    const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    live.socket = new WebSocket(`${wsProtocol}//${location.host}/ws/live`);
    live.socket.onopen = () => setLiveStatus('Connecting to realtime speech service…');
    live.socket.onmessage = async (event) => {
      if (state.live !== live) return;
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === 'ready') {
        try {
          await startLiveAudioGraph(live);
          const trackSettings = live.stream?.getAudioTracks?.()[0]?.getSettings?.() || {};
          const suppression = trackSettings.noiseSuppression === undefined
            ? 'browser default' : (trackSettings.noiseSuppression ? 'noise suppression on' : 'noise suppression off');
          setLiveStatus(live.audioMode === 'group' ? `Group Mode · ${suppression}` : `Focus Mode · ${suppression}`, true);
          updateAudioModeUI();
          button.disabled = false;
        } catch (err) {
          setError(`Could not start microphone audio: ${err.message || 'audio setup failed'}`);
          await stopLiveTranslation(live);
        }
      } else if (message.type === 'partial') {
        if (message.text) {
          $('live-partial').textContent = message.text;
          $('live-caption-state').textContent = 'Hearing Tamil · live partial';
        }
      } else if (message.type === 'final') {
        const finalText = String(message.text || '').trim();
        if (finalText) handleLiveFinal(live, finalText);
        $('live-partial').textContent = finalText || 'Listening…';
        $('live-caption-state').textContent = 'Phrase captured · translating';
      } else if (message.type === 'error') {
        setError(message.detail || 'Live speech recognition failed.');
        finishLiveSession(live, 'Connection error');
      } else if (message.type === 'session_end') {
        finishLiveSession(live, 'Live translation stopped');
      }
    };
    live.socket.onerror = () => {
      if (state.live === live) setError('The live connection failed. Check that the server is running and your Sarvam API key has realtime speech access.');
    };
    live.socket.onclose = () => {
      if (state.live === live && !live.finished) {
        finishLiveSession(live, live.stopping ? 'Live translation stopped' : 'Connection closed');
      }
    };
  } catch (err) {
    await releaseLiveAudio(live);
    button.disabled = false;
    button.textContent = '● Start live translation';
    setLiveStatus('Microphone unavailable', false);
    setError(`Could not start live translation: ${err.message || 'microphone permission denied'}. Allow microphone access and try again.`);
  }
}

async function stopLiveTranslation(live) {
  if (!live || live.stopping) return;
  live.stopping = true;
  live.active = false;
  await stopGroupRecording(live);
  await releaseLiveAudio(live);
  $('live-start-button').disabled = true;
  $('live-start-button').textContent = 'Finishing last phrase…';
  $('live-caption-state').textContent = 'Finalizing the last phrase';
  setLiveStatus('Finishing current utterance…', true);
  if (live.socket && live.socket.readyState === WebSocket.OPEN) {
    try { live.socket.send(JSON.stringify({ event: 'end' })); }
    catch { finishLiveSession(live); return; }
    live.stopTimer = setTimeout(() => finishLiveSession(live, 'Stopped · final results may still finish'), 12000);
  } else {
    finishLiveSession(live);
  }
}

function createLiveEntry(live, sourceText, voiceChoice, style, inputKind) {
  const empty = $('live-feed').querySelector('.live-empty');
  if (empty) empty.remove();
  const card = document.createElement('article');
  card.className = 'live-entry';
  const top = document.createElement('div'); top.className = 'live-entry-top';
  const label = document.createElement('span'); label.className = 'live-entry-label'; label.textContent = `PHRASE ${live.history.length + 1}`;
  const status = document.createElement('span'); status.className = 'live-entry-status'; status.textContent = 'Translating…';
  top.append(label, status);
  const source = document.createElement('p'); source.className = 'live-entry-source'; source.textContent = sourceText;
  const grid = document.createElement('div'); grid.className = 'live-entry-translations';
  const enWrap = document.createElement('div'); enWrap.className = 'live-entry-translation en';
  const enLabel = document.createElement('small'); enLabel.textContent = 'ENGLISH';
  const english = document.createElement('p'); english.textContent = 'Translation pending…';
  enWrap.append(enLabel, english);
  const hiWrap = document.createElement('div'); hiWrap.className = 'live-entry-translation hi';
  const hiLabel = document.createElement('small'); hiLabel.textContent = 'हिन्दी';
  const hindi = document.createElement('p'); hindi.textContent = 'अनुवाद प्रतीक्षारत…';
  hiWrap.append(hiLabel, hindi);
  grid.append(enWrap, hiWrap);
  const footer = document.createElement('div'); footer.className = 'live-entry-footer';
  const voiceStatus = document.createElement('span'); voiceStatus.textContent = voiceChoice === 'off' ? 'Captions only' : 'Voice queued';
  footer.append(voiceStatus);
  card.append(top, source, grid, footer);
  $('live-feed').append(card);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  return { sourceText, card, status, english, hindi, voiceStatus, footer, voiceChoice, style, inputKind, englishText: '', hindiText: '', notes: '' };
}

function handleLiveFinal(live, sourceText) {
  const style = $('style-select').value;
  const inputKind = 'mixed';
  const voiceChoice = $('live-voice-select').value;
  const previousEntries = live.history.slice(-5);
  const item = createLiveEntry(live, sourceText, voiceChoice, style, inputKind);
  live.history.push(item);
  live.translationQueue = live.translationQueue.then(async () => {
    try {
      const previousContext = previousEntries.map((entry) => {
        const translated = [entry.englishText ? `English: ${entry.englishText}` : '', entry.hindiText ? `Hindi: ${entry.hindiText}` : ''].filter(Boolean).join(' | ');
        return `Tamil: ${entry.sourceText}${translated ? `\n${translated}` : ''}`;
      }).join('\n');
      const manualContext = $('context-text').value.trim();
      const memoryContext = previousContext ? `Recent conversation in this live session:\n${previousContext}` : '';
      const context = [manualContext.slice(0, 1800), memoryContext.slice(-1800)].filter(Boolean).join('\n\n').slice(0, 3900);
      const result = await apiFetch('/api/translate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: sourceText, context, style: item.style, input_kind: item.inputKind })
      });
      item.englishText = result.english;
      item.hindiText = result.hindi;
      item.notes = result.notes || '';
      item.english.textContent = result.english;
      item.hindi.textContent = result.hindi;
      item.status.textContent = item.notes ? `Translated · ${item.notes}` : 'Translated';
      item.voiceStatus.textContent = item.voiceChoice === 'off' ? 'Captions only' : 'Preparing voice…';
      if (item.voiceChoice !== 'off') queueLiveVoice(live, item);
    } catch (err) {
      item.status.textContent = 'Translation failed';
      item.english.textContent = 'Could not translate this phrase.';
      item.hindi.textContent = 'इस वाक्यांश का अनुवाद नहीं हो सका।';
      item.voiceStatus.textContent = err.message || 'Translation error';
    }
  });
}

function queueLiveVoice(live, item) {
  const jobs = item.voiceChoice === 'both'
    ? [{ text: item.englishText, language: 'en-IN', label: 'English' }, { text: item.hindiText, language: 'hi-IN', label: 'Hindi' }]
    : [{ text: item.voiceChoice === 'hi' ? item.hindiText : item.englishText, language: item.voiceChoice === 'hi' ? 'hi-IN' : 'en-IN', label: item.voiceChoice === 'hi' ? 'Hindi' : 'English' }];
  live.voiceQueue = live.voiceQueue.then(async () => {
    for (const job of jobs) {
      try {
        item.voiceStatus.textContent = `Generating ${job.label} voice…`;
        const result = await apiFetch('/api/speak', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: job.text, language: job.language })
        });
        const audio = new Audio(result.audio_data_url);
        item.footer.append(audio);
        audio.controls = true;
        audio.className = 'live-audio-control';
        try {
          await audio.play();
          item.voiceStatus.textContent = `Playing ${job.label} voice`;
          await new Promise((resolve) => { audio.onended = resolve; audio.onerror = resolve; });
        } catch {
          item.voiceStatus.textContent = `${job.label} voice ready · press play`;
          audio.classList.remove('hidden');
        }
      } catch (err) {
        item.voiceStatus.textContent = `Voice error: ${err.message || 'unavailable'}`;
      }
    }
  }).catch((err) => { item.voiceStatus.textContent = `Voice error: ${err.message || 'unavailable'}`; });
}

$('live-start-button').addEventListener('click', startLiveTranslation);
$('live-processing-mode').addEventListener('change', updateAudioModeUI);
$('live-noise-suppression').addEventListener('change', updateAudioModeUI);
$('live-echo-cancellation').addEventListener('change', updateAudioModeUI);
$('live-speaker-count').addEventListener('change', updateAudioModeUI);
$('analyze-speakers-button').addEventListener('click', analyzeGroupSpeakers);
updateAudioModeUI();
$('clear-live-button').addEventListener('click', () => {
  if (state.live && (state.live.active || state.live.stopping)) {
    setError('Stop live translation before clearing the current conversation context.');
    return;
  }
  if (state.live) state.live.history = [];
  $('live-feed').innerHTML = '<div class="live-empty">Start live translation, then speak Tamil. Completed phrases and their English/Hindi translations will appear here.</div>';
  $('live-partial').textContent = 'Your Tamil speech will appear here as you speak.';
  $('live-caption-state').textContent = 'Waiting for microphone';
  setError('');
});

refreshHealth();
