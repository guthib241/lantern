"""Live talk to the avatar (S15): push to talk, transcribed as you speak by Muse Voice Transcribe in realtime.

The panel streams microphone audio (raw 16-bit little-endian mono PCM at 24 kHz) to Lantern's own server over a
local websocket (`/talk`, token-checked like `/events`). The server opens the realtime endpoint and relays what it
hears back, so the API key never reaches the window:

  wss://api.meta.ai/v1/asr/realtime (dev.meta.ai/docs/speech-to-text, api-reference/voice/realtime):
    1. the first JSON frame is the handshake, within 10 s: {"mode": "PUSH_TO_TALK", "model", "audioEncoding":
       "PCM_24KHZ", "partialMode": "CUMULATIVE", "emitAudioProgress": false, "authorization": {"accessToken"}}.
       The key goes in the handshake; the HTTP Authorization header is ignored there.
    2. the acknowledgement {"sessionId"} (the only frame without a "type"), then audio as binary frames, paced at
       real time (a microphone is), never more than 5 s ahead.
    3. {"type": "endStream"} when the button is let go; keep reading until the server closes with 1000.
    In PUSH_TO_TALK a "transcript" event with "final": true carries the whole utterance; earlier ones are
    cumulative partials that replace each other.
The page says `accessToken` is "Bearer <key>"; the cookbook sends the bare key. LANTERN_TALK_TOKEN=bare switches.
Billed like any transcription, $0.18 per hour of audio, whole seconds (logged as Muse Voice Transcribe).
"""
from __future__ import annotations

import json
import os
from typing import Any, Awaitable, Callable

from . import models

REALTIME_URL = "wss://api.meta.ai/v1/asr/realtime"
RATE = 24_000
BYTES_PER_SECOND = RATE * 2
MAX_SECONDS = 120          # one push to talk; long dictation belongs in a recording (Meetings)


class TalkError(RuntimeError):
    """Live talk can't start or went wrong; the message is safe to show."""


def handshake(key: str, *, keywords: list[str] | None = None, language_bias: list[str] | None = None) -> dict:
    style = os.environ.get("LANTERN_TALK_TOKEN", "bearer").strip().lower()
    token = key if style == "bare" else f"Bearer {key}"
    out: dict = {"mode": "PUSH_TO_TALK", "model": models.VOICE, "audioEncoding": "PCM_24KHZ",
                 "partialMode": "CUMULATIVE", "emitAudioProgress": False, "authorization": {"accessToken": token}}
    if keywords:   # only when set: the server ignores unknown or empty fields silently
        out["keywords"] = keywords[:50]
    if language_bias:
        out["languageBias"] = language_bias
    return out


def event_of(message: Any) -> dict | None:
    """A server text frame as what the panel needs: {"type": "partial"|"final"|"error", "text"}; None to skip."""
    if isinstance(message, (bytes, bytearray)):
        return None
    try:
        data = json.loads(message)
    except ValueError:
        return None
    if not isinstance(data, dict) or "type" not in data:
        return None   # the handshake acknowledgement, or anything else without a type
    if data["type"] == "transcript":
        text = " ".join(str(data.get("transcript", "")).split())
        return {"type": "final" if data.get("final") else "partial", "text": text}
    if data["type"] == "error":
        return {"type": "error", "text": str(data.get("message", "the transcription service stopped"))[:200]}
    return None   # speech turns, progress and future event types: ignored


async def relay(audio: Callable[[], Awaitable[bytes | None]], send: Callable[[dict], Awaitable[None]], *,
                key: str, url: str | None = None, keywords: list[str] | None = None,
                connect: Callable | None = None) -> dict:
    """Stream audio chunks (None = the button was let go) to the realtime endpoint and pass events to `send`.
    Returns {"text": the final transcript, "audio_ms": how much audio was sent}."""
    if not key:
        raise TalkError(f"live talk needs {models.API_KEY_ENV} in this computer's environment (not the proxy)")
    if connect is None:
        import websockets
        connect = websockets.connect
    url = url or os.environ.get("LANTERN_TALK_URL", REALTIME_URL)
    import asyncio
    sent, final = 0, ""
    async with connect(url, open_timeout=15, max_size=None) as ws:
        await ws.send(json.dumps(handshake(key, keywords=keywords)))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if isinstance(ack, dict) and ack.get("type") == "error":
            raise TalkError(f"the transcription service refused: {str(ack.get('message', ''))[:160]}")

        async def pump() -> None:
            nonlocal sent
            while True:
                chunk = await audio()
                if chunk is None or sent >= MAX_SECONDS * BYTES_PER_SECOND:
                    await ws.send(json.dumps({"type": "endStream"}))
                    return
                if chunk:
                    await ws.send(chunk)
                    sent += len(chunk)

        sender = asyncio.create_task(pump())
        try:
            async for message in ws:
                ev = event_of(message)
                if ev is None:
                    continue
                await send(ev)
                if ev["type"] == "final":
                    final = ev["text"]
                elif ev["type"] == "error":
                    raise TalkError(ev["text"])
        finally:
            sender.cancel()
    return {"text": final, "audio_ms": sent * 1000 // BYTES_PER_SECOND}
