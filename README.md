# Tamil Context Translator (MVP)

A web app for contextual Tamil → English + Hindi translation with clip-based transcription and a realtime microphone mode.

## Features

- Tamil-script, Tanglish, and mixed-language text input
- Optional conversation context to help resolve idioms and ambiguous phrases
- Natural, casual, and formal translation styles
- English and Hindi output, plus a short meaning note
- Upload or record audio in the browser, transcribe it, edit the transcript, then translate
- Live microphone streaming with interim Tamil captions and phrase-by-phrase English/Hindi translations
- Recent in-session utterances are added as context for later phrases
- Optional auto-spoken English, Hindi, or both after each completed phrase
- Focus Mode: browser/device echo cancellation and noise-suppression constraints during live microphone capture
- Group Mode: live captions/translations plus temporary in-browser audio capture, with opt-in post-session speaker diarization and translated speaker segments
- Generate spoken English or Hindi audio on demand
- API keys remain on the FastAPI server; the app does not persist audio or translation history to a database

## Requirements

- Python 3.10+
- A Sarvam API subscription key with access to speech-to-text, chat completions, and text-to-speech
- Internet connection

## Run on Windows (PowerShell)

```powershell
cd tamil_context_translator
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Put your real key in `.env`:

```env
SARVAM_API_KEY=sk_your_real_key_here
```

Then start the app:

```powershell
python -m uvicorn app:app --reload
```

Open http://127.0.0.1:8000 in your browser.

## Run on macOS/Linux

```bash
cd tamil_context_translator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set SARVAM_API_KEY
python -m uvicorn app:app --reload
```

Open http://127.0.0.1:8000.

## Live translation mode

Choose the **Live** tab and click **Start live translation**. Allow browser microphone access. The app opens a protected server-side WebSocket to Sarvam's realtime speech-to-text endpoint (`saaras:v3-realtime`), streams mono 16 kHz PCM16, shows interim Tamil captions, and translates finalized utterances into English and Hindi. It includes the previous five finalized phrases as conversation context where available. The Auto-speak selector can speak English, Hindi, both sequentially, or captions only.

Translation happens phrase-by-phrase after the realtime speech service detects a pause. It is not simultaneous word-for-word interpretation: contextual translation is more reliable when the service has a completed phrase. Auto-speech incurs additional text-to-speech API usage for every completed phrase. The API key stays on the FastAPI backend.

## Audio notes

- The synchronous Saaras REST workflow is designed for short clips (roughly 30 seconds). This starter enforces a 15 MB upload limit.
- Longer recordings should be split into smaller segments or moved to Sarvam's batch speech API.
- Microphone access generally requires `localhost`/HTTPS and browser permission.
- The transcript is shown for editing before translation because speech recognition can mishear names, slang, and noisy speech.

## API routes

- `GET /api/health` — checks app state and whether a key is configured
- `POST /api/translate` — JSON: `text`, optional `context`, `style`, and `input_kind`
- `POST /api/transcribe` — multipart upload field `file`
- `POST /api/speak` — JSON: `text`, `language` (`en-IN` or `hi-IN`)
- `WS /ws/live` — live microphone PCM stream proxied to Sarvam realtime speech recognition

## Translation quality and privacy

This is a prototype, not a guarantee of perfect translation. The contextual model can still misread dialects, implied meaning, and ambiguous expressions. Evaluate with fluent Tamil speakers and build a reviewed test set before relying on it for important communication. Text/audio is sent to the configured cloud provider for processing; do not submit sensitive information unless your provider settings and policies are appropriate.

## Troubleshooting

- **“API key is not configured”**: make sure `.env` is next to `app.py`, and restart Uvicorn after editing.
- **403/provider authentication error**: check the API key, account permissions, and endpoint/model access.
- **Audio errors**: try a clear recording under 30 seconds in WAV, MP3, M4A, or WebM format.
- **No microphone prompt**: use `http://127.0.0.1:8000` and allow microphone access in browser settings.

Audio recording compatibility note: browser MIME types such as `audio/webm;codecs=opus` are normalized to `audio/webm` before being sent to the speech provider.


## Focus Mode and Group Mode

- **Focus Mode (real-time):** uses browser/device echo cancellation and noise-suppression constraints on microphone capture. Browser support varies and the browser may ignore requested constraints. The application checks actual track settings when available.
- **Group Mode (real-time captions, post-session speaker labels):** sends microphone PCM to the existing Sarvam real-time WebSocket exactly as before. Simultaneously, the browser holds a temporary MediaRecorder recording in memory. Audio is uploaded only when the user stops and presses Analyze speakers.
- **Speaker diarization is not real-time in this integration.** The backend submits the clip to Sarvam's Batch Speech-to-Text API with diarization enabled, polls the batch job, and retrieves transcript segments with speaker IDs and timestamps. Completed segments are then translated into English and Hindi. Requires Batch STT access and may incur additional API charges.
- **Noise and music limitations:** browser noise suppression can help with steady environmental noise, and echo cancellation can reduce playback leakage. They are not guaranteed to remove music or television. This version does not run a dedicated neural music-removal or overlapping-speech-separation model.
- Group Mode is limited to 15 MB per session recording in this prototype. Batch analysis requires a clear enough recording; overlapping speech can be omitted or attributed incorrectly.
- The speaker-analysis endpoint caps uploads at two concurrent batch jobs and five submissions per app instance per hour to reduce accidental API-cost spikes. This is an in-memory prototype guard, not a substitute for user authentication or a durable distributed rate limiter.
- Group recordings are held in browser memory while the page remains open. They are uploaded only when the user selects Analyze speakers. The backend writes received bytes to a temporary directory for the batch job and removes that directory after processing; job status/results remain in process memory only.
