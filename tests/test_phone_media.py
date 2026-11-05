"""Phone photo + voice inputs (Nov 1): /m/api/photo, /m/api/voice and the circled-image route.

All model work is faked: core.photo / core.check_work are replaced on the test core, and the
voice converter/transcriber come from a fake hub exactly where server.py looks for them
(hub.converter / hub.transcriber, the WhatsApp pipeline's own seams). No real API, no ffmpeg.
"""
import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
from fastapi.testclient import TestClient

import lantern.server as server
from lantern import phone as phone_view
from lantern.pairing import PairingStore
from lantern.server import create_app
from lantern.voice import TranscribeError
from test_pairing_phone import H, device_header, make_core, pair

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes"
PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes"
WEBP = b"RIFF" + b"\x10\x00\x00\x00" + b"WEBP" + b"webp-bytes"
WAV = b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE" + b"fmt wav-bytes"


def make_hub_app(tmp_path, hub=None, core=None):
    core = core or make_core(tmp_path)
    store = PairingStore(tmp_path / "devices.json", clock=lambda: 1_000_000.0, code_gen=lambda: "123456")
    app = create_app(core, "test-token", hub=hub, pairing=store)
    client = TestClient(app)
    client.post("/pair/start", headers=H)
    tok = pair(client).json()["device_token"]
    return core, client, device_header(tok)


def fake_hub(transcript="remind me to call Priya", converter=None, transcriber=None):
    def convert(src, dst):
        Path(dst).write_bytes(b"converted-wav")
        return Path(dst)

    def transcribe(wav, keywords=None):
        return {"transcript": transcript, "audioDurationMs": 1200}

    return NS(channels={}, converter=converter or convert,
              transcriber=transcriber or transcribe, keywords=lambda: ["Lantern", "Priya"])


def capture_submit(core):
    """Route transcripts/goals like core.submit without queueing agent work."""
    seen = []

    def submit(goal):
        seen.append(goal)
        return core.memory.start_task(goal).id

    core.submit = submit
    return seen


# ---- photo: auth, sniffing, size, routing ------------------------------------------------

def test_photo_and_voice_and_image_need_device_auth(tmp_path):
    core, c, good = make_hub_app(tmp_path)
    for path, body, ctype in (("/m/api/photo", JPEG, "image/jpeg"), ("/m/api/voice", WAV, "audio/wav")):
        assert c.post(path, content=body, headers={"Content-Type": ctype}).status_code == 401
        bad = {"Content-Type": ctype, "X-Lantern-Device": "wrong"}
        assert c.post(path, content=body, headers=bad).status_code == 401
    assert c.get("/m/api/tasks/1/image").status_code == 401
    assert c.get("/m/api/tasks/1/image", headers={"X-Lantern-Device": "wrong"}).status_code == 401


def test_photo_routes_to_scan_with_caption_words(tmp_path):
    core, c, dev = make_hub_app(tmp_path)
    seen = []
    core.photo = lambda image, words="": seen.append((image, words)) or 41
    core.check_work = lambda image: (_ for _ in ()).throw(AssertionError("not check-work"))
    r = c.post("/m/api/photo?text=renew%20my%20permit", content=JPEG,
               headers={**dev, "Content-Type": "image/jpeg"})
    assert r.status_code == 200 and r.json() == {"task_id": 41, "image_type": "image/jpeg"}
    assert seen == [(JPEG, "renew my permit")]


def test_photo_check_my_work_caption_routes_to_check_work(tmp_path):
    core, c, dev = make_hub_app(tmp_path)
    seen = []
    core.photo = lambda image, words="": (_ for _ in ()).throw(AssertionError("not scan"))
    core.check_work = lambda image: seen.append(image) or 42
    r = c.post("/m/api/photo?text=please%20check%20my%20work", content=PNG,
               headers={**dev, "Content-Type": "image/png"})
    assert r.status_code == 200 and r.json()["task_id"] == 42 and seen == [PNG]


def test_photo_type_is_sniffed_not_trusted(tmp_path):
    core, c, dev = make_hub_app(tmp_path)
    core.photo = lambda image, words="": 1
    # the client claims JPEG but the bytes are text: refused, and nothing is routed
    r = c.post("/m/api/photo", content=b"not a photo at all",
               headers={**dev, "Content-Type": "image/jpeg"})
    assert r.status_code == 415
    # webp bytes under a lying name are accepted as webp
    core.check_work = lambda image: 7
    r = c.post("/m/api/photo?text=check%20my%20work", content=WEBP,
               headers={**dev, "Content-Type": "image/jpeg"})
    assert r.status_code == 200 and r.json()["image_type"] == "image/webp"
    assert phone_view.sniff_image(JPEG) == (".jpg", "image/jpeg")
    assert phone_view.sniff_image(b"GIF89a....") is None


def test_photo_size_limit_and_empty_refused(tmp_path, monkeypatch):
    core, c, dev = make_hub_app(tmp_path)
    core.photo = lambda image, words="": 1
    monkeypatch.setattr(server, "PHONE_PHOTO_MAX_BYTES", 8)
    r = c.post("/m/api/photo", content=JPEG, headers={**dev, "Content-Type": "image/jpeg"})
    assert r.status_code == 413
    monkeypatch.undo()
    r = c.post("/m/api/photo", content=b"", headers={**dev, "Content-Type": "image/jpeg"})
    assert r.status_code == 400


def test_photo_pipeline_refusal_is_plain(tmp_path):
    core, c, dev = make_hub_app(tmp_path)

    def refuse(image, words=""):
        raise ValueError("photos need Lantern's scheduler (lantern --serve)")

    core.photo = refuse
    r = c.post("/m/api/photo", content=JPEG, headers={**dev, "Content-Type": "image/jpeg"})
    assert r.status_code == 409 and "scheduler" in r.json()["detail"]


# ---- voice: pipeline, plain failures, routing --------------------------------------------

def test_voice_transcript_is_shown_and_routed_like_text(tmp_path):
    core, c, dev = make_hub_app(tmp_path, hub=fake_hub())
    seen = capture_submit(core)
    r = c.post("/m/api/voice", content=b"OggS fake voice bytes", headers={**dev, "Content-Type": "audio/ogg"})
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"] == "remind me to call Priya"
    assert body["message"] == "🎙 remind me to call Priya" and body["audio_ms"] == 1200
    assert seen == ["remind me to call Priya"]   # handled like typed text, through _submit_goal
    assert core.memory.get_task(body["task_id"]).goal == "remind me to call Priya"


def test_voice_without_ffmpeg_or_key_says_so_plainly(tmp_path):
    def no_ffmpeg(src, dst):
        raise TranscribeError("ffmpeg is not installed")

    core, c, dev = make_hub_app(tmp_path, hub=fake_hub(converter=no_ffmpeg))
    r = c.post("/m/api/voice", content=WAV, headers={**dev, "Content-Type": "audio/wav"})
    assert r.status_code == 503 and "ffmpeg is not installed" in r.json()["detail"]

    def no_key(wav, keywords=None):
        raise TranscribeError("set MODEL_API_KEY first")

    (tmp_path / "b").mkdir()
    core2, c2, dev2 = make_hub_app(tmp_path / "b", hub=fake_hub(transcriber=no_key))
    r = c2.post("/m/api/voice", content=WAV, headers={**dev2, "Content-Type": "audio/wav"})
    assert r.status_code == 503 and "no model key is set" in r.json()["detail"]


def test_voice_empty_transcript_and_bad_type_and_size(tmp_path, monkeypatch):
    core, c, dev = make_hub_app(tmp_path, hub=fake_hub(transcript="  "))
    seen = capture_submit(core)
    r = c.post("/m/api/voice", content=WAV, headers={**dev, "Content-Type": "audio/wav"})
    assert r.status_code == 422 and "couldn't make out" in r.json()["detail"] and seen == []

    core2, c2, dev2 = make_hub_app(tmp_path, hub=fake_hub())
    r = c2.post("/m/api/voice", content=b"plain text", headers={**dev2, "Content-Type": "text/plain"})
    assert r.status_code == 415
    r = c2.post("/m/api/voice", content=b"not wav bytes", headers={**dev2, "Content-Type": "audio/wav"})
    assert r.status_code == 415   # a .wav claim on non-WAV bytes
    monkeypatch.setattr(server, "PHONE_VOICE_MAX_BYTES", 4)
    r = c2.post("/m/api/voice", content=WAV, headers={**dev2, "Content-Type": "audio/wav"})
    assert r.status_code == 413


def test_voice_demo_core_gets_scripted_transcript(tmp_path):
    core, c, dev = make_hub_app(tmp_path)   # no hub: rehearsal path in the server
    core.demo = True
    seen = capture_submit(core)
    r = c.post("/m/api/voice", content=WAV, headers={**dev, "Content-Type": "audio/wav"})
    assert r.status_code == 200 and r.json()["transcript"] and seen == [r.json()["transcript"]]
    assert r.json()["message"].startswith("🎙 ")


# ---- the event loop stays responsive; oversize uploads are refused early ------------------

def test_slow_voice_and_photo_do_not_block_the_event_loop(tmp_path):
    """ffmpeg/transcription and photo routing run off the loop: /health answers mid-flight."""
    started = threading.Event()

    def slow_convert(src, dst):
        started.set()
        time.sleep(1.5)
        Path(dst).write_bytes(b"converted-wav")
        return Path(dst)

    core, c, dev = make_hub_app(tmp_path, hub=fake_hub(converter=slow_convert))
    capture_submit(core)
    photo_started = threading.Event()

    def slow_photo(image, words=""):
        photo_started.set()
        time.sleep(1.5)
        return 55

    core.photo = slow_photo

    async def run():
        transport = httpx.ASGITransport(app=c.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            voice = asyncio.create_task(ac.post("/m/api/voice", content=WAV,
                                                headers={**dev, "Content-Type": "audio/wav"}))
            assert await asyncio.to_thread(started.wait, 5)
            t0 = time.monotonic()
            health = await ac.get("/health")
            assert health.status_code == 200 and time.monotonic() - t0 < 1.0 and not voice.done()
            assert (await voice).status_code == 200

            photo = asyncio.create_task(ac.post("/m/api/photo", content=JPEG,
                                                headers={**dev, "Content-Type": "image/jpeg"}))
            assert await asyncio.to_thread(photo_started.wait, 5)
            t0 = time.monotonic()
            health = await ac.get("/health")
            assert health.status_code == 200 and time.monotonic() - t0 < 1.0 and not photo.done()
            resp = await photo
            assert resp.status_code == 200 and resp.json()["task_id"] == 55

    asyncio.run(run())


def test_upload_declared_content_length_refused_before_reading(tmp_path, monkeypatch):
    core, c, dev = make_hub_app(tmp_path, hub=fake_hub())
    monkeypatch.setattr(server, "PHONE_PHOTO_MAX_BYTES", 8)
    routed = []
    core.photo = lambda image, words="": routed.append(image) or 1
    # a tiny body lying about nothing: the declared length alone is over the limit
    r = c.post("/m/api/photo", content=b"\xff\xd8\xff",
               headers={**dev, "Content-Type": "image/jpeg", "Content-Length": "999"})
    assert r.status_code == 413 and routed == []
    # multipart: refused on its declared length before the form is parsed or routed
    r = c.post("/m/api/photo", files={"image": ("x.jpg", JPEG, "image/jpeg")}, headers=dev)
    assert r.status_code == 413 and routed == []

    monkeypatch.setattr(server, "PHONE_VOICE_MAX_BYTES", 8)
    heard = []
    core2, c2, dev2 = make_hub_app(tmp_path, hub=fake_hub(
        converter=lambda src, dst: heard.append(src) or Path(dst)))
    capture_submit(core2)
    r = c2.post("/m/api/voice", content=b"RIFF",
                headers={**dev2, "Content-Type": "audio/wav", "Content-Length": "999"})
    assert r.status_code == 413 and heard == []


def test_upload_streamed_body_over_limit_refused_without_content_length(tmp_path, monkeypatch):
    core, c, dev = make_hub_app(tmp_path, hub=fake_hub())
    monkeypatch.setattr(server, "PHONE_PHOTO_MAX_BYTES", 8)
    routed = []
    core.photo = lambda image, words="": routed.append(image) or 1

    def photo_chunks():
        yield b"\xff\xd8"
        yield b"\xff\xe0xxxx"
        yield b"more-bytes-that-put-it-over"

    r = c.post("/m/api/photo", content=photo_chunks(), headers={**dev, "Content-Type": "image/jpeg"})
    assert r.status_code == 413 and routed == []   # chunked: no Content-Length to trust

    monkeypatch.setattr(server, "PHONE_VOICE_MAX_BYTES", 8)
    heard = []
    core2, c2, dev2 = make_hub_app(tmp_path, hub=fake_hub(
        converter=lambda src, dst: heard.append(src) or Path(dst)))
    capture_submit(core2)

    def voice_chunks():
        yield b"RIFF"
        yield b"\x24\x00\x00\x00"
        yield b"WAVE-and-more"

    r = c2.post("/m/api/voice", content=voice_chunks(), headers={**dev2, "Content-Type": "audio/wav"})
    assert r.status_code == 413 and heard == []


# ---- circled image: only this task's, only Lantern's, never private -----------------------

def circled_task(core, tmp_path, private=False, outside=False):
    task = core.memory.start_task("Check my work")
    media = Path(core.cfg.media_dir)
    media.mkdir(parents=True, exist_ok=True)
    path = (tmp_path / "outside.png") if outside else (media / "checked-1.png")
    path.write_bytes(PNG)
    core.memory.log_step(task.id, "tool_result", {"name": "check_work", "result": {"image": str(path)}})
    core.memory.finish_task(task.id, "done", "Line 2 doesn't follow. I circled it on the photo.")
    if private:
        core.memory.mark_private(task.id)
    return task


def test_task_image_serves_circled_image_and_view_flags_it(tmp_path):
    core, c, dev = make_hub_app(tmp_path)
    task = circled_task(core, tmp_path)
    r = c.get(f"/m/api/tasks/{task.id}/image", headers=dev)
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"].startswith("image/png")
    view = c.get(f"/m/api/tasks/{task.id}", headers=dev).json()
    assert view["has_image"] is True
    listed = [t for t in c.get("/m/api/tasks", headers=dev).json() if t["id"] == task.id]
    assert listed and listed[0]["has_image"] is True


def test_task_image_refused_for_private_outside_and_other_tasks(tmp_path):
    core, c, dev = make_hub_app(tmp_path)
    private = circled_task(core, tmp_path, private=True)
    r = c.get(f"/m/api/tasks/{private.id}/image", headers=dev)
    assert r.status_code == 403 and PNG not in r.content
    view = c.get(f"/m/api/tasks/{private.id}", headers=dev).json()
    assert view["has_image"] is False

    outside = circled_task(core, tmp_path, outside=True)
    assert c.get(f"/m/api/tasks/{outside.id}/image", headers=dev).status_code == 404

    plain = core.memory.start_task("buy milk")
    assert c.get(f"/m/api/tasks/{plain.id}/image", headers=dev).status_code == 404  # not its image
    assert c.get("/m/api/tasks/9999/image", headers=dev).status_code == 404


# ---- static app: same shapes the browser runs, no external origins -------------------------

def test_static_app_has_photo_voice_and_no_external_origins(tmp_path):
    core, c, dev = make_hub_app(tmp_path)
    index = (Path(server.PHONE_DIR) / "index.html").read_text(encoding="utf-8")
    app_js = (Path(server.PHONE_DIR) / "app.js").read_text(encoding="utf-8")
    for name in ("index.html", "app.js", "style.css", "sw.js"):
        text = (Path(server.PHONE_DIR) / name).read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, name   # no external origins
    assert 'type="file"' in index and 'accept="image/*"' in index and 'capture="environment"' in index
    assert 'id="photo-input"' in index and 'id="mic"' in index
    assert "MediaRecorder" in app_js and "getUserMedia" in app_js
    assert "2000" in app_js and "image/jpeg" in app_js          # on-phone downscale to JPEG
    assert "/m/api/photo" in app_js and "/m/api/voice" in app_js and "/image" in app_js
    assert "60000" in app_js                                     # 60 s recording cap
    r = c.get("/m")
    assert "media-src 'self' blob:" in r.headers["Content-Security-Policy"]
    assert "img-src 'self' data: blob:" in r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
