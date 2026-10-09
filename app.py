from __future__ import annotations

import asyncio
import base64
import json
import os
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
