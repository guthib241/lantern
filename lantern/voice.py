"""Speech to text with Muse Voice Transcribe (one-shot file endpoint).

Request shape (dev.meta.ai/docs/api-reference/voice/transcribe and the cookbook
recipe 06_muse_voice/01_voice_api_fundamentals): POST {BASE_URL}/asr/transcribe,
multipart/form-data with two parts:
  request: JSON {"model", "mode", "audioEncoding": "WAV", "keywords"?, "languageBias"?}
  audio:   the WAV file (mono 16-bit, 16 kHz or 24 kHz)
Auth: `Authorization: Bearer $MODEL_API_KEY`. Limit: 10 minutes or 32 MB.
Response JSON: {"sessionId", "transcript", "audioDurationMs", "turns"?}.

Only the standard library is used; `post` can be swapped out in tests.
Audio files passed here are Lantern's own downloads (voice notes from channels,
kept under ~/.lantern/media), not user files, so they are read directly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from . import models

MAX_BYTES = 32 * 1024 * 1024
MODES = ("PUSH_TO_TALK", "ENDPOINTING", "DIARIZATION")

# post(url, body, headers) -> (status, body_bytes)
Poster = Callable[[str, bytes, dict], tuple[int, bytes]]


class TranscribeError(RuntimeError):
    pass


def _urllib_post(url: str, body: bytes, headers: dict, timeout: float = 120) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def build_multipart(request: dict, audio: bytes, filename: str) -> tuple[bytes, str]:
    """Encode the two parts; returns (body, content_type)."""
    boundary = "lantern-" + uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="request"\r\n'
        "Content-Type: application/json\r\n\r\n".encode()
        + json.dumps(request).encode() + b"\r\n",
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="audio"; filename="{filename}"\r\n'
        "Content-Type: audio/wav\r\n\r\n".encode()
        + audio + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe(path: str | Path, *, api_key: str | None = None, keywords: list[str] | None = None,
               language_bias: list[str] | None = None, mode: str = "PUSH_TO_TALK",
               base_url: str = models.BASE_URL, post: Poster | None = None) -> dict:
    """Transcribe a WAV file. Keywords bias recognition toward names (contacts, projects)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    key = api_key or os.environ.get(models.API_KEY_ENV)
    if not key:
        raise TranscribeError(f"set {models.API_KEY_ENV} first")
    path = Path(path)
    audio = path.read_bytes()
    if len(audio) > MAX_BYTES:
        raise TranscribeError(f"{path.name} is over 32 MB")
    request: dict = {"model": models.VOICE, "mode": mode, "audioEncoding": "WAV"}
    if keywords:
        request["keywords"] = keywords[:100]
    if language_bias:
        request["languageBias"] = language_bias
    body, ctype = build_multipart(request, audio, path.name)
    status, data = (post or _urllib_post)(
        base_url.rstrip("/") + models.TRANSCRIBE_PATH, body,
        {"Authorization": f"Bearer {key}", "Content-Type": ctype, "Accept": "application/json"},
    )
    if status != 200:
        raise TranscribeError(f"transcribe failed [{status}]: {data[:300].decode(errors='replace')}")
    return json.loads(data)


def to_wav16k(src: str | Path, dst: str | Path) -> Path:
    """Convert any audio (e.g. WhatsApp OGG/Opus voice notes) to 16 kHz mono 16-bit WAV with ffmpeg."""
    if not shutil.which("ffmpeg"):
        raise TranscribeError("ffmpeg is not installed")
    dst = Path(dst)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000",
         "-sample_fmt", "s16", str(dst)],
        check=True, timeout=120,
    )
    return dst
