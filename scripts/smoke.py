"""One real call per Meta model; prints OK / FAIL / SKIP for each.

    python scripts/smoke.py            # all models
    python scripts/smoke.py spark sam  # just these

Needs MODEL_API_KEY (skips everything without it). Costs well under $0.05 per run.
The Glimmer check runs only if LANTERN_GLIMMER_URL is set (local or Modal server).
Request shapes follow dev.meta.ai/docs and the meta-model-cookbook.
"""
from __future__ import annotations

import base64
import math
import os
import struct
import sys
import tempfile
import time
import wave
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lantern import models  # noqa: E402
from lantern.voice import transcribe  # noqa: E402


def tiny_png(size: int = 64) -> bytes:
    """A white square on a dark background, built with the standard library."""
    rows = []
    for y in range(size):
        row = bytearray([0])  # filter byte
        for x in range(size):
            inside = size // 4 <= x < 3 * size // 4 and size // 4 <= y < 3 * size // 4
            row += bytes([240, 240, 240] if inside else [30, 30, 60])
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def tone_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(seconds * rate))
        )
        w.writeframes(frames)
    return path


def client():
    from openai import OpenAI
    return OpenAI(base_url=models.BASE_URL, api_key=os.environ[models.API_KEY_ENV])


def check_text(model: str) -> str:
    r = client().responses.create(model=model, input="Reply with the single word: lantern")
    u = models.Usage.from_response(r.usage)
    return f"{(r.output_text or '').strip()[:40]!r} · ${models.token_cost(model, u):.5f}"


def check_image() -> str:
    r = client().responses.create(model=models.IMAGE, input="a small paper lantern icon, flat style, plain background")
    imgs = [o for o in r.output if getattr(o, "type", "") == "image_generation_call"]
    if not imgs:
        raise RuntimeError("no image_generation_call in output")
    return f"{len(base64.b64decode(imgs[0].result))} bytes · ${models.image_cost(1):.2f}"


def check_voice() -> str:
    with tempfile.TemporaryDirectory() as d:
        result = transcribe(tone_wav(Path(d) / "tone.wav"))
    return f"session {result.get('sessionId')} · {result.get('audioDurationMs')} ms"


def check_sam() -> str:
    url = "data:image/png;base64," + base64.b64encode(tiny_png()).decode()
    stream = client().responses.create(
        model=models.SAM, stream=True,
        input=[{"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "white square"},
                            {"type": "input_image", "image_url": url}]}],
    )
    last = None
    for event in stream:
        last = getattr(event, "type", None)
        if last in ("response.completed", "response.failed", "response.incomplete"):
            break
    if last != "response.completed":
        raise RuntimeError(f"stream ended with {last}")
    return f"completed · ${models.sam_cost(1):.4f}"


def check_glimmer() -> str:
    from openai import OpenAI
    url = os.environ["LANTERN_GLIMMER_URL"]
    c = OpenAI(base_url=url, api_key=os.environ.get("LANTERN_GLIMMER_KEY", "local"))
    r = c.chat.completions.create(model=os.environ.get("LANTERN_GLIMMER_MODEL", models.GLIMMER_LOCAL),
                                  messages=[{"role": "user", "content": "Reply with: lantern"}], max_tokens=10)
    return repr((r.choices[0].message.content or "").strip()[:40])


CHECKS = {
    "spark": (models.SPARK, lambda: check_text(models.SPARK)),
    "wonder": (models.WONDER, lambda: check_text(models.WONDER)),
    "image": (models.IMAGE, check_image),
    "voice": (models.VOICE, check_voice),
    "sam": (models.SAM, check_sam),
    "glimmer": ("glimmer (LANTERN_GLIMMER_URL)", check_glimmer),
}


def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    names = argv or list(CHECKS)
    failed = 0
    for name in names:
        label, fn = CHECKS[name]
        if name == "glimmer" and not os.environ.get("LANTERN_GLIMMER_URL"):
            print(f"SKIP {label}: LANTERN_GLIMMER_URL not set")
            continue
        if name != "glimmer" and not os.environ.get(models.API_KEY_ENV):
            print(f"SKIP {label}: {models.API_KEY_ENV} not set")
            continue
        t0 = time.time()
        try:
            detail = fn()
            print(f"OK   {label} ({time.time() - t0:.1f}s) {detail}")
        except Exception as e:  # report and keep going
            failed += 1
            print(f"FAIL {label} ({time.time() - t0:.1f}s) {type(e).__name__}: {str(e)[:200]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
