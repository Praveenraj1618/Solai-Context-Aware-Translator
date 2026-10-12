from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

import httpx
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SARVAM_BASE_URL = os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai").rstrip("/")
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "").strip()
CHAT_MODEL = os.getenv("SARVAM_CHAT_MODEL", "sarvam-105b")
TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v4-flash")
MAX_AUDIO_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 12000
MAX_DIARIZATION_AUDIO_BYTES = 15 * 1024 * 1024
MAX_ACTIVE_DIARIZATION_JOBS = 2
MAX_DIARIZATION_JOBS_PER_HOUR = 5
DIARIZATION_SUBMISSIONS: list[float] = []
DIARIZATION_TASKS: dict[str, dict] = {}

app = FastAPI(title="Tamil Context Translator", version="0.2.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class TranslateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    context: str = Field(default="", max_length=4000)
    style: Literal["natural", "casual", "formal"] = "natural"
    input_kind: Literal["tamil", "tanglish", "mixed"] = "tamil"


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2500)
    language: Literal["en-IN", "hi-IN"]


class SpeakerSegmentForTranslation(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    speaker: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=1800)
    start_time_seconds: float = Field(ge=0)
    end_time_seconds: float = Field(ge=0)


class SpeakerSegmentsTranslateRequest(BaseModel):
    segments: list[SpeakerSegmentForTranslation] = Field(min_length=1, max_length=20)
    context: str = Field(default="", max_length=3000)
    style: Literal["natural", "casual", "formal"] = "natural"


def api_headers() -> dict[str, str]:
    if not SARVAM_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Sarvam API key is not configured. Copy .env.example to .env and add your SARVAM_API_KEY.",
        )
    return {"api-subscription-key": SARVAM_API_KEY}


def extract_json_object(raw: str | None) -> dict:
    """Parse model JSON robustly, even if a model wraps it in a code fence."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("The translation model returned no text content. Please retry.")
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            result = json.loads(raw[start : end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    raise ValueError("The translation model returned an unreadable response. Please retry.")


@app.get("/")
def home() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "api_key_configured": bool(SARVAM_API_KEY), "chat_model": CHAT_MODEL}


@app.post("/api/translate")
async def translate(request: TranslateRequest) -> dict:
    source_text = request.text.strip()
    if not source_text:
        raise HTTPException(status_code=400, detail="Enter Tamil text or Tanglish to translate.")

    style_instructions = {
        "natural": "Use the most natural, context-appropriate everyday phrasing. Preserve the speaker's intent and register.",
        "casual": "Use natural casual, conversational language. Preserve slang and interpersonal tone where possible without sounding forced.",
        "formal": "Use clear, polite, grammatically standard language suitable for formal or professional situations.",
    }
    input_descriptions = {
        "tamil": "The input is primarily Tamil written in Tamil script, but it may contain English words.",
        "tanglish": "The input is Tamil written using Latin/Roman letters (Tanglish), possibly mixed with English. Infer the intended Tamil meaning rather than translating the Latin spellings literally.",
        "mixed": "The input may mix Tamil script, Tanglish, and English. Preserve the meaning of the full mixed-language utterance.",
    }
    context_block = request.context.strip() or "No prior conversation context was provided. Interpret the sentence on its own and do not invent missing details."

    system_prompt = (
        "You are a careful professional Tamil-to-English-and-Hindi translator. Your job is meaning-preserving translation, "
        "not word-for-word substitution. Understand colloquial Tamil, idioms, implied meaning, politeness, sarcasm, "
        "emotion and relationship cues when the text supports them. Use provided context to resolve ambiguity. "
        "Never invent facts or add details not supported by the input/context. If an ambiguity cannot be resolved, "
        "choose the most plausible translation and briefly mention the ambiguity in notes. Keep proper nouns, numbers, "
        "negation, conditions and intent accurate. Hindi output must be in Devanagari script. English should sound natural, "
        "not like a literal translation. Return valid JSON only with exactly these keys: english, hindi, literal_meaning, notes. "
        "The english and hindi values must be translations, not explanations. literal_meaning is a short plain-language explanation "
        "of the intended meaning, not a word-by-word gloss. notes may be an empty string when there is nothing important to flag."
    )
    user_prompt = (
        f"Input type: {input_descriptions[request.input_kind]}\n"
        f"Requested style: {request.style}. {style_instructions[request.style]}\n\n"
        f"Relevant conversation context (may be empty or partial):\n{context_block}\n\n"
        f"Source text:\n{source_text}\n\n"
        "Translate the source into both English (Indian English where suitable) and Hindi. Return only the required JSON object."
    )
    payload = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        # Short translations do not need a reasoning-token budget. Sarvam documents
        # that reasoning can consume max_tokens before message.content is generated.
        "reasoning_effort": None,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=15.0)) as client:
            response = await client.post(
                f"{SARVAM_BASE_URL}/v1/chat/completions",
                headers={**api_headers(), "Content-Type": "application/json"},
                json=payload,
            )
            if response.is_error:
                detail = _safe_provider_error(response)
                raise HTTPException(status_code=502, detail=f"Translation provider error: {detail}")
            data = response.json()
            content = _chat_message_content(data)

            # If JSON mode yields an empty assistant message, retry once with the same
            # JSON instructions in the prompt but without response_format. This also
            # lets the user see a clear provider diagnostic instead of a .strip() crash.
            if not content:
                fallback_payload = {key: value for key, value in payload.items() if key != "response_format"}
                response = await client.post(
                    f"{SARVAM_BASE_URL}/v1/chat/completions",
                    headers={**api_headers(), "Content-Type": "application/json"},
                    json=fallback_payload,
                )
                if response.is_error:
                    detail = _safe_provider_error(response)
                    raise HTTPException(status_code=502, detail=f"Translation provider error on retry: {detail}")
                data = response.json()
                content = _chat_message_content(data)

        if not content:
            raise HTTPException(status_code=502, detail=_empty_completion_detail(data))
        translated = extract_json_object(content)
    except HTTPException:
        raise
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach translation provider: {type(exc).__name__}. Please retry.") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc) or "Unexpected translation response.") from exc

    english = str(translated.get("english", "")).strip()
    hindi = str(translated.get("hindi", "")).strip()
    if not english or not hindi:
        raise HTTPException(status_code=502, detail="The model did not return both English and Hindi. Please retry.")
    return {
        "source_text": source_text,
        "english": english,
        "hindi": hindi,
        "literal_meaning": str(translated.get("literal_meaning", "")).strip(),
        "notes": str(translated.get("notes", "")).strip(),
        "model": CHAT_MODEL,
    }


def _chat_message_content(data: object) -> str | None:
    """Return assistant text from a chat-completion response, if present."""
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    # Be tolerant of providers that return text content as typed content parts.
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text", ""), str)]
        joined = "".join(parts).strip()
        return joined or None
    return None


def _empty_completion_detail(data: object) -> str:
    """Create useful diagnostics without returning the provider's full response body."""
    if not isinstance(data, dict):
        return "The translation API returned an unexpected response shape."
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    finish_reason = str(choice.get("finish_reason", "unknown"))
    message_fields = ", ".join(sorted(str(key) for key in message.keys())) or "none"
    request_id = data.get("id") or data.get("request_id") or "not provided"
    return (
        "The translation API returned no assistant text after a retry "
        f"(finish_reason={finish_reason}; message_fields={message_fields}; request_id={request_id}). "
        "Check the model/API response for this request and try again."
    )


def _safe_provider_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            err = payload.get("message") or payload.get("detail") or payload.get("error")
            if isinstance(err, dict):
                err = err.get("message") or err.get("detail")
            if err:
                return str(err)[:500]
    except ValueError:
        pass
    return f"HTTP {response.status_code}"


@app.post("/api/transcribe")
async def transcribe_audio(file: UploadFile = File(...)) -> dict:
    # Browsers may send a MIME type such as "audio/webm;codecs=opus".
    # The provider expects the base media type only (for example, "audio/webm"),
    # so strip codec parameters before validation and forwarding the upload.
    content_type = (file.content_type or "application/octet-stream").lower().split(";", 1)[0].strip()
    content_type = content_type or "application/octet-stream"
    allowed_types = (
        "audio/", "video/webm", "video/mp4", "application/octet-stream"
    )
    if not any(content_type.startswith(item) for item in allowed_types):
        raise HTTPException(status_code=415, detail="Upload an audio file such as WAV, MP3, M4A, OGG, or WebM.")
    audio_bytes = await file.read(MAX_AUDIO_BYTES + 1)
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="The uploaded audio file is empty.")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio is too large (maximum 15 MB for this prototype).")

    # Saaras synchronous REST is intended for clips up to about 30 seconds.
    filename = Path(file.filename or "recording.webm").name
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=15.0)) as client:
            response = await client.post(
                f"{SARVAM_BASE_URL}/speech-to-text",
                headers=api_headers(),
                data={"model": "saaras:v4", "language_code": "ta-IN"},
                files={"file": (filename, audio_bytes, content_type)},
            )
        if response.is_error:
            raise HTTPException(status_code=502, detail=f"Speech recognition provider error: {_safe_provider_error(response)}")
        data = response.json()
    except HTTPException:
        raise
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach speech recognition provider: {type(exc).__name__}. Please retry.") from exc
    transcript = str(data.get("transcript", "")).strip()
    if not transcript:
        raise HTTPException(status_code=502, detail="No transcript was returned. Try a clearer or shorter audio clip.")
    return {"transcript": transcript, "language_code": data.get("language_code", "ta-IN"), "request_id": data.get("request_id")}


def _run_sarvam_diarization_sync(audio_bytes: bytes, filename: str, num_speakers: int | None) -> dict:
    """Run the provider's batch diarization in a worker thread; audio is temporary and deleted."""
    try:
        from sarvamai import SarvamAI
    except ImportError as exc:
        raise RuntimeError("Speaker analysis SDK is not installed. Redeploy after installing requirements.txt.") from exc

    allowed_extensions = {".webm", ".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".mp4", ".aiff"}
    extension = Path(filename or "group-session.webm").suffix.lower()
    if extension not in allowed_extensions:
        extension = ".webm"

    with tempfile.TemporaryDirectory(prefix="solai-diarization-") as temp_dir:
        root = Path(temp_dir)
        audio_path = root / ("group-session" + extension)
        audio_path.write_bytes(audio_bytes)

        client = SarvamAI(api_subscription_key=SARVAM_API_KEY)
        params = {
            "model": "saaras:v4",
            "mode": "transcribe",
            "language_code": "ta-IN",
            "with_diarization": True,
        }
        if num_speakers is not None:
            params["num_speakers"] = num_speakers

        job = client.speech_to_text_job.create_job(**params)
        job.upload_files(file_paths=[str(audio_path)])
        job.start()
        job.wait_until_complete(poll_interval=3, timeout=360)

        file_results = job.get_file_results()
        if isinstance(file_results, dict):
            successful = file_results.get("successful") or []
            failed = file_results.get("failed") or []
        else:
            successful = getattr(file_results, "successful", []) or []
            failed = getattr(file_results, "failed", []) or []
        if not successful:
            reason = "Batch speech recognition did not complete successfully."
            if failed and isinstance(failed[0], dict) and failed[0].get("error_message"):
                reason = "Batch speech recognition failed: " + str(failed[0]["error_message"])[:250]
            raise RuntimeError(reason)

        output_dir = root / "output"
        output_dir.mkdir(exist_ok=True)
        job.download_outputs(output_dir=str(output_dir))
        result_files = sorted(output_dir.rglob("*.json"))
        if not result_files:
            raise RuntimeError("Speaker analysis completed, but no transcript output file was returned.")

        payload = json.loads(result_files[0].read_text(encoding="utf-8"))
        diarized = payload.get("diarized_transcript") or {}
        entries = diarized.get("entries") if isinstance(diarized, dict) else []
        if not isinstance(entries, list) or not entries:
            raise RuntimeError("The provider returned no speaker-labelled segments. Try a clearer recording with distinct speaker turns.")

        labels: dict[str, str] = {}
        segments: list[dict] = []
        for entry in entries[:80]:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("transcript") or "").strip()
            if not text:
                continue
            raw_speaker = str(entry.get("speaker_id", "unknown"))
            if raw_speaker not in labels:
                labels[raw_speaker] = "Speaker " + str(len(labels) + 1)
            try:
                start_seconds = max(0.0, float(entry.get("start_time_seconds", 0) or 0))
            except (TypeError, ValueError):
                start_seconds = 0.0
            try:
                end_seconds = max(start_seconds, float(entry.get("end_time_seconds", start_seconds) or start_seconds))
            except (TypeError, ValueError):
                end_seconds = start_seconds
            segments.append({
                "id": str(len(segments)),
                "speaker": labels[raw_speaker],
                "text": text[:1800],
                "start_time_seconds": start_seconds,
                "end_time_seconds": end_seconds,
            })

        if not segments:
            raise RuntimeError("The provider returned no usable speaker-labelled speech.")
        return {
            "transcript": str(payload.get("transcript") or ""),
            "speaker_count": len(labels),
            "segments": segments,
            "request_id": payload.get("request_id"),
        }


async def _diarization_worker(task_id: str, audio_bytes: bytes, filename: str, num_speakers: int | None) -> None:
    task = DIARIZATION_TASKS.get(task_id)
    if not task:
        return
    task["status"] = "running"
    try:
        result = await asyncio.to_thread(_run_sarvam_diarization_sync, audio_bytes, filename, num_speakers)
        task = DIARIZATION_TASKS.get(task_id)
        if task is not None:
            task.update({"status": "completed", "result": result})
    except Exception as exc:
        task = DIARIZATION_TASKS.get(task_id)
        if task is not None:
            safe_detail = (
                str(exc)[:350]
                if isinstance(exc, (RuntimeError, TimeoutError, ValueError))
                else "Check that your Sarvam account has Batch Speech-to-Text access and review the server logs."
            )
            task.update({"status": "failed", "detail": safe_detail, "error_type": type(exc).__name__})
    finally:
        task = DIARIZATION_TASKS.get(task_id)
        if task is not None:
            task.pop("task", None)


@app.post("/api/diarize")
async def start_speaker_diarization(
    file: UploadFile = File(...),
    num_speakers: int | None = Form(default=None, ge=2, le=20),
) -> dict:
    """Queue post-session speaker diarization; raw audio is not saved by this app."""
    if not SARVAM_API_KEY:
        raise HTTPException(status_code=503, detail="SARVAM_API_KEY is not configured.")
    now = time.monotonic()
    DIARIZATION_SUBMISSIONS[:] = [stamp for stamp in DIARIZATION_SUBMISSIONS if now - stamp < 3600]
    active_jobs = sum(1 for task in DIARIZATION_TASKS.values() if task.get("status") in {"queued", "running"})
    if active_jobs >= MAX_ACTIVE_DIARIZATION_JOBS:
        raise HTTPException(status_code=429, detail="Speaker analysis is busy. Wait for the current batch job to finish, then retry.")
    if len(DIARIZATION_SUBMISSIONS) >= MAX_DIARIZATION_JOBS_PER_HOUR:
        raise HTTPException(status_code=429, detail="Speaker-analysis limit reached for this app this hour. Please retry later.")
    content_type = (file.content_type or "application/octet-stream").lower().split(";", 1)[0].strip()
    if not (content_type.startswith("audio/") or content_type == "video/webm" or content_type == "application/octet-stream"):
        raise HTTPException(status_code=415, detail="Upload a supported audio recording (WebM, WAV, MP3, M4A, OGG, or FLAC).")
    audio_bytes = await file.read(MAX_DIARIZATION_AUDIO_BYTES + 1)
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="The group recording is empty.")
    if len(audio_bytes) > MAX_DIARIZATION_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="The group recording is larger than 15 MB. Use a shorter session.")

    # Keep only a small number of finished in-memory results for this prototype.
    if len(DIARIZATION_TASKS) >= 12:
        for old_id, old_task in list(DIARIZATION_TASKS.items()):
            if old_task.get("status") in {"completed", "failed"}:
                DIARIZATION_TASKS.pop(old_id, None)
            if len(DIARIZATION_TASKS) < 8:
                break

    task_id = uuid.uuid4().hex
    filename = Path(file.filename or "group-session.webm").name
    DIARIZATION_SUBMISSIONS.append(time.monotonic())
    DIARIZATION_TASKS[task_id] = {"status": "queued"}
    background_task = asyncio.create_task(_diarization_worker(task_id, audio_bytes, filename, num_speakers))
    DIARIZATION_TASKS[task_id]["task"] = background_task
    return {"job_id": task_id, "status": "queued"}


@app.get("/api/diarize/{job_id}")
async def get_speaker_diarization(job_id: str) -> dict:
    task = DIARIZATION_TASKS.get(job_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Speaker analysis job was not found. Start the analysis again.")
    return {key: value for key, value in task.items() if key != "task"}


@app.post("/api/translate-speaker-segments")
async def translate_speaker_segments(request: SpeakerSegmentsTranslateRequest) -> dict:
    style_guidance = {
        "natural": "Use natural, context-appropriate phrasing.",
        "casual": "Use natural casual conversation and preserve interpersonal tone.",
        "formal": "Use clear, polite, professional phrasing.",
    }
    segment_data = [
        {"id": segment.id, "speaker": segment.speaker, "text": segment.text}
        for segment in request.segments
    ]
    system_prompt = (
        "You translate finalized Tamil speech segments into English and Hindi. Translate each item independently while "
        "using context to resolve ambiguity. Preserve each id exactly, do not merge or reorder segments, keep speaker labels "
        "attached to the same utterance, preserve negation/names/numbers, and never invent content. Hindi must be in Devanagari. "
        "Return valid JSON only with one key: translations. Its value must be an array of objects with exactly id, english, hindi."
    )
    context = request.context.strip() or "No additional conversation context."
    user_prompt = (
        "Style: " + request.style + ". " + style_guidance[request.style] + "\n"
        "Conversation context:\n" + context + "\n\n"
        "Translate these segments in order; retain each id and speaker:\n"
        + json.dumps(segment_data, ensure_ascii=False)
    )
    payload = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "reasoning_effort": None,
        "max_tokens": 4500,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
            response = await client.post(
                f"{SARVAM_BASE_URL}/v1/chat/completions",
                headers={**api_headers(), "Content-Type": "application/json"},
                json=payload,
            )
            if response.is_error:
                raise HTTPException(status_code=502, detail=f"Speaker translation provider error: {_safe_provider_error(response)}")
            data = response.json()
            content = _chat_message_content(data)
            if not content:
                fallback = {key: value for key, value in payload.items() if key != "response_format"}
                response = await client.post(
                    f"{SARVAM_BASE_URL}/v1/chat/completions",
                    headers={**api_headers(), "Content-Type": "application/json"},
                    json=fallback,
                )
                if response.is_error:
                    raise HTTPException(status_code=502, detail=f"Speaker translation provider error on retry: {_safe_provider_error(response)}")
                data = response.json()
                content = _chat_message_content(data)
        if not content:
            raise HTTPException(status_code=502, detail=_empty_completion_detail(data))
        translated = extract_json_object(content)
        rows = translated.get("translations")
        if not isinstance(rows, list):
            raise HTTPException(status_code=502, detail="The model did not return a translations array for the speaker segments.")
        by_id = {
            str(row.get("id")): row
            for row in rows
            if isinstance(row, dict) and row.get("id") is not None
        }
        output = []
        for segment in request.segments:
            row = by_id.get(segment.id)
            if row is None:
                raise HTTPException(status_code=502, detail="The model omitted a speaker segment translation. Retry the analysis.")
            english = str(row.get("english") or "").strip()
            hindi = str(row.get("hindi") or "").strip()
            if not english or not hindi:
                raise HTTPException(status_code=502, detail="The model returned an empty speaker segment translation. Retry the analysis.")
            output.append({"id": segment.id, "english": english, "hindi": hindi})
        return {"translations": output}
    except HTTPException:
        raise
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach the translation provider: {type(exc).__name__}. Please retry.") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc) or "Unexpected speaker translation response.") from exc



@app.post("/api/speak")
async def speak(request: SpeakRequest) -> JSONResponse:
    payload = {
        "model": TTS_MODEL,
        "text": request.text.strip(),
        "language_code": request.language,
        "output_audio_codec": "wav",
        "pace": 1.0,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=15.0)) as client:
            response = await client.post(
                f"{SARVAM_BASE_URL}/text-to-speech",
                headers={**api_headers(), "Content-Type": "application/json"},
                json=payload,
            )
        if response.is_error:
            raise HTTPException(status_code=502, detail=f"Speech generation provider error: {_safe_provider_error(response)}")
        data = response.json()
        audios = data.get("audios") or []
        if not audios:
            raise HTTPException(status_code=502, detail="The voice provider did not return audio.")
        # A data URL lets the browser play the generated WAV without storing files on the server.
        audio_data_url = "data:audio/wav;base64," + audios[0]
        return JSONResponse({"audio_data_url": audio_data_url, "request_id": data.get("request_id")})
    except HTTPException:
        raise
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach text-to-speech provider: {type(exc).__name__}. Please retry.") from exc


@app.websocket("/ws/live")
async def live_speech_stream(websocket: WebSocket) -> None:
    """Proxy low-latency microphone PCM to Sarvam realtime STT without exposing the API key."""
    await websocket.accept()
    if not SARVAM_API_KEY:
        await websocket.send_json({"type": "error", "detail": "Sarvam API key is not configured. Add SARVAM_API_KEY to .env and restart the server."})
        await websocket.close(code=1011)
        return

    params = urlencode({
        "language_code": "ta-IN",
        "model": "saaras:v3-realtime",
        "stream_type": "balanced",
        "mode": "transcribe",
        "encoding": "linear16",
        "sample_rate": "16000",
        "endpointing": "vad",
        "threshold": "0.3",
        "silence_duration_ms": "500",
        "min_speech_duration_ms": "250",
    })
    provider_url = f"{SARVAM_BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')}/speech-to-text-realtime/ws?{params}"

    async def safe_send(payload: dict) -> None:
        try:
            await websocket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            pass

    try:
        async with websockets.connect(
            provider_url,
            additional_headers={"api-subscription-key": SARVAM_API_KEY},
            ping_interval=20,
            ping_timeout=20,
            close_timeout=3,
            max_size=2 * 1024 * 1024,
        ) as provider:
            await websocket.send_json({"type": "ready", "provider": "saaras:v3-realtime"})

            async def browser_to_provider() -> None:
                while True:
                    message = await websocket.receive_text()
                    try:
                        event_data = json.loads(message)
                    except json.JSONDecodeError:
                        await safe_send({"type": "error", "detail": "The live audio client sent an invalid message."})
                        continue
                    if not isinstance(event_data, dict):
                        continue
                    event_type = event_data.get("event")
                    if event_type == "audio_input":
                        encoded_audio = event_data.get("audio")
                        if isinstance(encoded_audio, str) and encoded_audio:
                            await provider.send(json.dumps({"event": "audio_input", "audio": encoded_audio}))
                    elif event_type == "ping":
                        await provider.send(json.dumps({"event": "ping"}))
                    elif event_type == "end":
                        # Feed ~800 ms of real-time silence so server-side VAD can finalize
                        # the last utterance before ending the provider session.
                        silence = base64.b64encode(b"\x00" * 3200).decode("ascii")  # 100 ms, 16 kHz mono PCM16
                        for _ in range(8):
                            await provider.send(json.dumps({"event": "audio_input", "audio": silence}))
                            await asyncio.sleep(0.1)
                        await provider.send(json.dumps({"event": "end"}))
                        return

            async def provider_to_browser() -> None:
                saw_session_end = False
                async for raw_message in provider:
                    try:
                        event_data = json.loads(raw_message)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(event_data, dict):
                        continue
                    event_type = event_data.get("event", "")
                    if event_type == "transcript.partial":
                        await websocket.send_json({
                            "type": "partial",
                            "text": str(event_data.get("text") or event_data.get("transcript") or ""),
                        })
                    elif event_type == "transcript.final":
                        await websocket.send_json({
                            "type": "final",
                            "text": str(event_data.get("text") or event_data.get("transcript") or ""),
                        })
                    elif event_type == "error":
                        await websocket.send_json({
                            "type": "error",
                            "detail": str(event_data.get("message") or "Realtime speech recognition failed."),
                            "code": event_data.get("code"),
                            "fatal": bool(event_data.get("is_fatal")),
                        })
                        if event_data.get("is_fatal"):
                            raise RuntimeError("The real-time speech provider reported a fatal error.")
                    elif event_type == "session.end":
                        saw_session_end = True
                        await websocket.send_json({"type": "session_end", "audio_duration_s": event_data.get("audio_duration_s")})
                        return
                    elif event_type in {"vad.speech_start", "vad.speech_end"}:
                        await websocket.send_json({"type": event_type})
                if not saw_session_end:
                    raise RuntimeError("The speech provider closed the realtime connection unexpectedly.")

            client_task = asyncio.create_task(browser_to_provider())
            provider_task = asyncio.create_task(provider_to_browser())
            tasks = {client_task, provider_task}
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    error = task.exception() if not task.cancelled() else None
                    if error and not isinstance(error, WebSocketDisconnect):
                        raise error
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    except WebSocketDisconnect:
        pass
    except websockets.exceptions.InvalidStatus as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"Sarvam realtime connection was rejected (HTTP {status}). Check API key permissions and realtime speech access." if status else "Sarvam realtime connection was rejected. Check API key permissions and realtime speech access."
        await safe_send({"type": "error", "detail": detail})
    except Exception as exc:
        # Avoid returning headers, credentials, or other provider request internals to the browser.
        await safe_send({"type": "error", "detail": f"Live speech connection failed ({type(exc).__name__}). Check network access, API permissions, and try again."})
    finally:
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
