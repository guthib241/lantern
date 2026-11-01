"""Nov 1: circled check-work images sent back over WhatsApp/Telegram, and the persistent daily mark.

All HTTP is faked; request shapes follow the official docs (see docs/DECISIONS.md, Nov 1):
WhatsApp media upload + image message, Telegram sendPhoto (multipart).
"""
import hashlib
import json
import time
from pathlib import Path

import pytest

from fakes import FakeClient
from lantern.channels import FakeChannel, TelegramChannel, WhatsAppChannel
from lantern.channels.base import Channel, ChannelError
from lantern.channels.hub import ChannelHub
from lantern.config import Config
from lantern.core import Core

PNG = b"\x89PNG\r\n\x1a\n" + b"circled-photo-bytes"
OWNER = "919876543210"


def make_hub(tmp_path, channels):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "a.db"), str(tmp_path / "z.json")
    cfg.media_dir, cfg.profile_path = str(tmp_path / "media"), str(tmp_path / "profile.json")
    core = Core(cfg, FakeClient([]), approval_timeout=10)
    hub = ChannelHub(core, channels).start(listen=False)
    return core, hub


def media_file(tmp_path, name="checked-1.png", data=PNG):
    folder = Path(tmp_path / "media")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


# ---- WhatsApp: upload then image message -------------------------------------------------
class FakeGraphMedia:
    """graph.facebook.com stand-in: records raw requests (multipart bodies are not JSON)."""

    def __init__(self):
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body or b""})
        if url.endswith("/media"):
            return 200, b'{"id": "MEDIA9"}'
        if url.endswith("/messages"):
            return 200, b'{"messages": [{"id": "wamid.out"}]}'
        return 404, b"{}"

    def sent_json(self):
        return [json.loads(r["body"]) for r in self.requests if r["url"].endswith("/messages")]


def test_whatsapp_send_image_uploads_then_sends_with_caption(tmp_path):
    graph = FakeGraphMedia()
    wa = WhatsAppChannel("tok", "PNID", "+91 98765 43210", http=graph)
    path = media_file(tmp_path)
    wa.send_image(path, "Line 3 doesn't follow. I circled it on the photo.")
    upload, send = graph.requests
    assert upload["method"] == "POST"
    assert upload["url"] == "https://graph.facebook.com/v23.0/PNID/media"
    assert upload["headers"]["Authorization"] == "Bearer tok"
    assert upload["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    body = upload["body"]
    assert b'name="messaging_product"' in body and b"whatsapp" in body
    assert b'name="type"' in body and b"image/png" in body
    assert b'name="file"; filename="checked-1.png"' in body and PNG in body
    assert graph.sent_json() == [{
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": "+91 98765 43210",
        "type": "image",
        "image": {"id": "MEDIA9", "caption": "Line 3 doesn't follow. I circled it on the photo."}}]


def test_whatsapp_caption_truncated_and_jpeg_allowed(tmp_path):
    graph = FakeGraphMedia()
    wa = WhatsAppChannel("tok", "PNID", OWNER, http=graph)
    path = media_file(tmp_path, "checked-1.jpg", b"\xff\xd8\xff jpeg-bytes")
    wa.send_image(path, "x" * 1500)
    assert graph.sent_json()[0]["image"]["caption"] == "x" * 1024
    assert b"image/jpeg" in graph.requests[0]["body"]


def test_whatsapp_refuses_bad_type_and_oversize(tmp_path):
    graph = FakeGraphMedia()
    wa = WhatsAppChannel("tok", "PNID", OWNER, http=graph)
    gif = media_file(tmp_path, "checked-1.gif", b"GIF89a")
    with pytest.raises(ChannelError):
        wa.send_image(gif, "hi")
    big = media_file(tmp_path, "checked-2.png", b"x" * (5 * 1024 * 1024 + 1))
    with pytest.raises(ChannelError):
        wa.send_image(big, "hi")
    assert graph.requests == []   # refused before anything left


# ---- Telegram: sendPhoto ------------------------------------------------------------------
class FakeTelegramMedia:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body or b""})
        return 200, json.dumps({"ok": True, "result": {"message_id": 7}}).encode()


def test_telegram_send_photo_multipart_with_caption(tmp_path):
    http = FakeTelegramMedia()
    tg = TelegramChannel("BOT", "4242", http=http)
    path = media_file(tmp_path)
    tg.send_image(path, "Line 3 doesn't follow.")
    (call,) = http.calls
    assert call["url"] == "https://api.telegram.org/botBOT/sendPhoto"
    assert call["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    body = call["body"]
    assert b'name="chat_id"' in body and b"4242" in body
    assert b'name="caption"' in body and b"Line 3 doesn't follow." in body
    assert b'name="photo"; filename="checked-1.png"' in body and PNG in body


def test_telegram_caption_and_size_limits(tmp_path):
    http = FakeTelegramMedia()
    tg = TelegramChannel("BOT", "4242", http=http)
    tg.send_image(media_file(tmp_path), "y" * 1500)
    assert b"y" * 1024 in http.calls[0]["body"] and b"y" * 1025 not in http.calls[0]["body"]
    big = media_file(tmp_path, "checked-2.png", b"x" * (10 * 1024 * 1024 + 1))
    with pytest.raises(ChannelError):
        tg.send_image(big, "hi")
    bad = media_file(tmp_path, "checked-3.webp", b"webp")
    with pytest.raises(ChannelError):
        tg.send_image(bad, "hi")
    assert len(http.calls) == 1


# ---- base fallback + simulated phone --------------------------------------------------------
def test_base_channel_falls_back_to_text_and_fake_records_image(tmp_path):
    class Plain(Channel):
        name = "plain"

        def __init__(self):
            self.got = []

        def send_text(self, text):
            self.got.append(text)

        def send_buttons(self, text, buttons):
            raise AssertionError("unused")

    plain = Plain()
    plain.send_image(tmp_path / "x.png", "circled it")
    assert plain.got == ["📷 image: circled it"]

    phone = FakeChannel()
    path = media_file(tmp_path)
    phone.send_image(path, "circled it")
    (msg,) = phone.messages()
    assert msg["type"] == "image" and msg["text"] == "📷 image: circled it"
    assert msg["caption"] == "circled it" and msg["name"] == "checked-1.png" and msg["bytes"] == len(PNG)


# ---- hub: media guard, scrub, audit, private refusal ----------------------------------------
def test_hub_sends_only_media_images_scrubs_and_audits(tmp_path):
    graph = FakeGraphMedia()
    wa = WhatsAppChannel("tok", "PNID", OWNER, http=graph)
    core, hub = make_hub(tmp_path, [wa])
    try:
        path = media_file(tmp_path)
        caption = "Found AKIAABCDEFGHIJKLMNOP circled"
        assert hub.say_image("whatsapp", path, caption)
        assert hub.wait_idle(10)
        sent = graph.sent_json()
        assert sent and sent[0]["image"]["caption"] == "Found [REDACTED:aws_access_key] circled"
        (row,) = [e for e in core.audit.entries() if e["endpoint"] == "whatsapp.send_image"]
        assert row["destination"] == "https://graph.facebook.com" and row["bytes"] == len(PNG)
        assert row["sha256"] == hashlib.sha256(PNG).hexdigest()
        assert "AKIA" not in row["preview"] and "circled" in row["preview"]
    finally:
        hub.stop()
        core.shutdown()


def test_hub_refuses_outside_media_and_private_tasks(tmp_path):
    phone = FakeChannel()
    core, hub = make_hub(tmp_path, [phone])
    try:
        inside = media_file(tmp_path)
        outside = tmp_path / "secret.png"
        outside.write_bytes(PNG)
        assert not hub.say_image("fake", outside, "nope")
        assert not hub.say_image("fake", inside, "nope", task_id=None, event={
            "reply": "private words", "reply_public": "kept on the computer"})
        task = core.memory.start_task("[fake] private check")
        core.memory.mark_private(task.id)
        assert not hub.say_image("fake", inside, "nope", task_id=task.id)
        assert hub.wait_idle(10)
        assert phone.messages() == [] and hub.errors   # every refusal was recorded, nothing sent
    finally:
        hub.stop()
        core.shutdown()


def test_checkwork_done_event_sends_image_with_reply_as_caption(tmp_path):
    phone = FakeChannel()
    core, hub = make_hub(tmp_path, [phone])
    try:
        path = media_file(tmp_path)
        task = core.memory.start_task("Check my work")
        hub.origin[task.id] = "fake"
        hub._on_event({"type": "checkwork", "task_id": task.id, "image": str(path),
                       "reply": "Line 3 doesn't follow. I circled it on the photo."})
        hub._on_event({"type": "done", "id": task.id, "status": "done",
                       "reply": "Line 3 doesn't follow. I circled it on the photo.", "summary": "checked"})
        assert hub.wait_idle(10)
        (msg,) = [m for m in phone.messages() if m["type"] == "image"]
        assert msg["caption"] == "Line 3 doesn't follow. I circled it on the photo."
        assert not [m for m in phone.messages() if m["type"] == "text"]   # no duplicate text reply
    finally:
        hub.stop()
        core.shutdown()


def test_checkwork_private_done_event_gets_text_only(tmp_path):
    phone = FakeChannel()
    core, hub = make_hub(tmp_path, [phone])
    try:
        path = media_file(tmp_path)
        task = core.memory.start_task("Check my work")
        core.memory.mark_private(task.id)
        hub.origin[task.id] = "fake"
        hub._on_event({"type": "checkwork", "task_id": task.id, "image": str(path), "reply": "secret reply"})
        hub._on_event({"type": "done", "id": task.id, "status": "done",
                       "reply": "secret reply", "summary": "s"})
        assert hub.wait_idle(10)
        assert [m["type"] for m in phone.messages()] == ["text"]
        assert phone.messages()[0]["text"] == "secret reply"   # _photo_done events carry no reply_public
        assert not [m for m in phone.messages() if m["type"] == "image"]
    finally:
        hub.stop()
        core.shutdown()


# ---- daily summary: the sent mark survives a restart -----------------------------------------
def test_daily_sent_mark_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("LANTERN_DAILY_SUMMARY", "21:00")
    phone = FakeChannel()
    core, hub = make_hub(tmp_path, [phone])
    lt = time.localtime()
    at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 22, 0, 0, 0, 0, -1))
    try:
        assert hub.check_daily(now=at)
        assert hub.wait_idle(10)
        assert [m.get("text", "") for m in phone.messages() if m["type"] == "text"]
    finally:
        hub.stop()
        core.shutdown()

    phone2 = FakeChannel()
    core2, hub2 = make_hub(tmp_path, [phone2])   # same Lantern home: a restart
    try:
        assert not hub2.check_daily(now=at + 600)
        assert hub2.wait_idle(10)
        assert phone2.messages() == []
    finally:
        hub2.stop()
        core2.shutdown()
