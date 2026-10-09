# Manual smoke test

1. Start the server using the README instructions.
2. Open `http://127.0.0.1:8000` and verify the UI loads.
3. Without `.env`, verify the header shows `API key needed`; pressing Translate should display a clear setup error rather than crash.
4. With a valid API key, try text: `நான் பாத்துக்கறேன், நீ கவலைப்படாத.` and compare natural vs formal styles.
5. Add the previous message as context and compare the intended meaning.
6. Upload a short Tamil audio clip, transcribe, correct the transcript, then translate.
7. Press Listen for English and Hindi and confirm a playable WAV response is returned.
8. Verify `/api/health` returns JSON.

9. Open the **Live** tab and click **Start live translation**; allow microphone access.
10. Speak a short Tamil phrase and confirm interim captions update before the full phrase appears.
11. Wait for the English and Hindi translations after a short pause.
12. Try Auto-speak as English, Hindi, Both, then Off; confirm audio can play and note that each phrase may call the TTS API.
13. Stop live translation while speaking; verify the last phrase is finalized and the microphone indicator stops.
