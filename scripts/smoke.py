"""One real call per Meta model; prints OK / FAIL / SKIP for each.

    python scripts/smoke.py            # all models
    python scripts/smoke.py spark sam  # just these

Needs MODEL_API_KEY (skips everything without it). Costs well under $0.05 per run.
The Glimmer check runs only if LANTERN_GLIMMER_URL is set (local or Modal server).
The `cua` check (computer use, M7) needs Playwright + Chromium: it shows a calibration page
with one button at a known spot, asks the forms model (LANTERN_FORMS_MODEL, default Spark)
to click it with the native computer tool and with the cookbook's function tools, and prints
which protocol works and whether coordinates come back as pixels or 0..1000.
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


CALIBRATION = """<!doctype html><body style="margin:0;width:1280px;height:800px;background:#f6f3fa;font:20px sans-serif">
<p style="padding:30px">Calibration page. Click the orange OK button.</p>
<button style="position:absolute;left:1040px;top:620px;width:120px;height:60px;background:#f0782f;color:#fff;
border:0;border-radius:12px;font-size:22px">OK</button></body>"""
TARGET = (1100, 650)  # centre of the button, in screenshot pixels


def _units(x: float, y: float) -> str:
    if abs(x - TARGET[0]) < 70 and abs(y - TARGET[1]) < 70:
        return "pixel"
    if abs(x - TARGET[0] * 1000 / 1280) < 70 and abs(y - TARGET[1] * 1000 / 800) < 70:
        return "normalized"
    return "unclear"


def recommend(native: str, function: str) -> str:
    """The .env line for what the calibration found (names as lantern/config.py reads them)."""
    if native == "pixel":
        return "LANTERN_CUA_PROTOCOL=native"
    if native == "normalized":
        return "LANTERN_CUA_PROTOCOL=native LANTERN_CUA_COORDS=normalized"
    if function == "normalized":
        return "LANTERN_CUA_PROTOCOL=function"
    if function == "pixel":
        return "LANTERN_CUA_PROTOCOL=function LANTERN_CUA_COORDS=pixel"
    return "neither protocol clicked the button: paste this whole line into the session"


def check_cua() -> str:
    import json as _json
    from lantern.forms.computer import CUA_RULES, FUNCTION_EXTRA, FUNCTION_TOOLS, NATIVE_TOOL, call_actions, data_url, parse_action
    from lantern.forms.driver import BrowserDriver, chromium_executable
    model = models.check_model(os.environ.get("LANTERN_FORMS_MODEL", models.SPARK))
    with tempfile.TemporaryDirectory() as d, BrowserDriver(Path(d) / "profile", executable_path=chromium_executable()) as b:
        b.page.set_content(CALIBRATION)
        shot = data_url(b.screenshot())
    goal = [{"role": "user", "content": [{"type": "input_text", "text": "Click the orange OK button."},
                                         {"type": "input_image", "image_url": shot}]}]
    report = []
    native_units = function_units = "none"
    try:
        r = client().responses.create(model=model, instructions=CUA_RULES, input=goal, tools=[NATIVE_TOOL])
        calls = [o for o in r.output if getattr(o, "type", "") == "computer_call"]
        acts = [a for c in calls for a in call_actions(c) if a["kind"] == "click"]
        raw = [getattr(c, "model_dump", lambda **k: c)(exclude_none=True) for c in calls]
        native_units = _units(acts[0]["x"], acts[0]["y"]) if acts else "no click"
        report.append(f"native: {native_units} {_json.dumps(raw, default=str)[:300]}")
    except Exception as e:
        report.append(f"native: rejected ({type(e).__name__}: {str(e)[:120]})")
    r = client().responses.create(model=model, instructions=CUA_RULES + FUNCTION_EXTRA, input=goal,
                                  tools=FUNCTION_TOOLS, parallel_tool_calls=False)
    fcs = [o for o in r.output if getattr(o, "type", "") == "function_call" and o.name == "computer.computer"]
    if fcs:
        a = parse_action(_json.loads(fcs[0].arguments))
        function_units = _units(a.get('x', -1), a.get('y', -1))
        report.append(f"function: {function_units} {fcs[0].arguments[:120]}")
    else:
        report.append("function: no computer.computer call")
    return f"{model}: " + " | ".join(report) + f"  →  put in .env: {recommend(native_units, function_units)}"


CHECKS = {
    "spark": (models.SPARK, lambda: check_text(models.SPARK)),
    "wonder": (models.WONDER, lambda: check_text(models.WONDER)),
    "image": (models.IMAGE, check_image),
    "voice": (models.VOICE, check_voice),
    "sam": (models.SAM, check_sam),
    "glimmer": ("glimmer (LANTERN_GLIMMER_URL)", check_glimmer),
    "cua": ("computer use (forms model)", check_cua),
}


def main(argv: list[str]) -> int:
    from lantern.config import load_env
    load_env()   # the repo's .env, whether an editor or PowerShell (UTF-16) saved it
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
    from lantern.cli import safe_console
    safe_console()   # ✓ and ✗ must not crash a piped Windows console
    raise SystemExit(main(sys.argv[1:]))
