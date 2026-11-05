"""M8: WhatsApp webhook + Telegram polling + simulated phone, all offline with fake HTTP layers."""
import hashlib
import hmac
import json
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call
from test_routines import Both
from lantern import models
from lantern.channels import (ChannelHub, FakeChannel, Incoming, TelegramChannel, WhatsAppChannel,
                              channels_from_env, verify_signature)
from lantern.channels.fake import FAKE_OWNER
from lantern.config import Config
from lantern.core import Core
from lantern.server import create_app

OWNER = "919876543210"
SECRET = "app-secret"
VERIFY = "verify-me"
H = {"X-Lantern-Token": "t"}


class FakeGraph:
    """Stands in for graph.facebook.com: records sends, serves one voice note."""

    def __init__(self, audio=b"OggS-fake-audio"):
        self.requests = []
        self.audio = audio

    def __call__(self, method, url, headers, body):
        self.requests.append({"method": method, "url": url, "headers": headers,
                              "json": json.loads(body) if body else None})
        if url.endswith("/messages"):
            return 200, b'{"messages": [{"id": "wamid.out"}]}'
        if url.endswith("/media-123"):
            return 200, json.dumps({"url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123",
                                    "mime_type": "audio/ogg; codecs=opus", "id": "media-123"}).encode()
        if "lookaside" in url:
            return 200, self.audio
        return 404, b"{}"

    def sent(self, kind=None):
        out = [r["json"] for r in self.requests if r["url"].endswith("/messages")]
        return [m for m in out if kind is None or m["type"] == kind]

    def texts(self):
        return [m["text"]["body"] for m in self.sent("text")]


def make(tmp_path, script, *, transcriber=None, converter=None, channels=None, chat=None):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "a.db"), str(tmp_path / "z.json")
    cfg.media_dir, cfg.profile_path = str(tmp_path / "media"), str(tmp_path / "profile.json")
    client = Both(FakeClient(script), chat) if chat is not None else FakeClient(script)
    core = Core(cfg, client, approval_timeout=10)
    graph = FakeGraph()
    wa = WhatsAppChannel("tok", "PNID", "+91 98765 43210", app_secret=SECRET, verify_token=VERIFY, http=graph)
    hub = ChannelHub(core, channels or [wa], transcriber=transcriber, converter=converter).start(listen=False)
    return core, hub, graph, TestClient(create_app(core, "t", hub)), root


def payload(*messages):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550001111", "phone_number_id": "PNID"},
        "contacts": [{"profile": {"name": "Asha"}, "wa_id": OWNER}], "messages": list(messages)}}]}]}


def text(body, frm=OWNER, mid="wamid.1"):
    return {"from": frm, "id": mid, "timestamp": "1761480000", "type": "text", "text": {"body": body}}


def button(bid, mid="wamid.b"):
    return {"from": OWNER, "id": mid, "timestamp": "1761480001", "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": bid, "title": "✅ Approve"}}}


def post(http, body, secret=SECRET):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return http.post("/webhook/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig,
                                                                 "Content-Type": "application/json"})


def settle(core, hub, rounds=3):
    for _ in range(rounds):
        assert hub.wait_idle(10) and core.wait_idle(10)


def wait_for(predicate, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---- webhook security --------------------------------------------------------------
def test_handshake(tmp_path):
    core, hub, graph, http, _ = make(tmp_path, [])
    ok = http.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY,
                                               "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    bad = http.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                "hub.challenge": "x"})
    assert bad.status_code == 403


def test_bad_signature_rejected_and_nothing_runs(tmp_path):
    core, hub, graph, http, _ = make(tmp_path, [])
    assert post(http, payload(text("hi")), secret="wrong").status_code == 401
    raw = json.dumps(payload(text("hi"))).encode()
    assert http.post("/webhook/whatsapp", content=raw).status_code == 401
    settle(core, hub)
    assert core.memory.recent_tasks() == [] and graph.sent() == []
    assert not verify_signature(raw, "sha1=abc", SECRET)


def test_owner_text_becomes_task_and_reply_comes_back(tmp_path):
    core, hub, graph, http, _ = make(tmp_path, [([], "All quiet on your laptop.")])
    assert post(http, payload(text("what's up?"))).json() == {"ok": True}
    settle(core, hub)
    (task,) = core.memory.recent_tasks()
    assert task.goal == "[whatsapp] what's up?" and task.status == "done"
    assert graph.texts() == [f"🏮 On it (task #{task.id}).", "All quiet on your laptop."]
    msg = graph.sent()[0]
    assert msg["to"] == "+91 98765 43210" and msg["messaging_product"] == "whatsapp"
    assert graph.requests[0]["url"] == "https://graph.facebook.com/v23.0/PNID/messages"
    assert graph.requests[0]["headers"]["Authorization"] == "Bearer tok"


def test_strangers_are_ignored_and_retries_deduplicated(tmp_path):
    core, hub, graph, http, _ = make(tmp_path, [([], "ok")])
    post(http, payload(text("delete everything", frm="15550009999")))
    post(http, payload(text("hello", mid="wamid.same")))
    post(http, payload(text("hello", mid="wamid.same")))  # Meta redelivers
    settle(core, hub)
    assert hub.ignored == 1 and len(core.memory.recent_tasks()) == 1


def test_status_updates_are_ignored(tmp_path):
    core, hub, graph, http, _ = make(tmp_path, [])
    status_only = payload()
    status_only["entry"][0]["changes"][0]["value"]["statuses"] = [{"id": "wamid.out", "status": "read"}]
    del status_only["entry"][0]["changes"][0]["value"]["messages"]
    assert post(http, status_only).status_code == 200
    settle(core, hub)
    assert core.memory.recent_tasks() == []


# ---- approvals as buttons --------------------------------------------------------------
def approval_flow(tmp_path):
    return make(tmp_path, [
        ([call("write_file", "c1", path="todo.txt", content="buy milk")], ""),
        ([call("finish", "c2", status="done", summary="wrote todo.txt")], ""),
        ([], "Done. Milk is on the list."),
    ])


def card(graph):
    assert wait_for(lambda: graph.sent("interactive"))
    msg = graph.sent("interactive")[0]["interactive"]
    return msg, {b["reply"]["title"]: b["reply"]["id"] for b in msg["action"]["buttons"]}


def test_approve_button_runs_the_action(tmp_path):
    core, hub, graph, http, root = approval_flow(tmp_path)
    post(http, payload(text("add milk to my todo")))
    msg, ids = card(graph)
    assert msg["type"] == "button" and list(ids) == ["✅ Approve", "❌ Deny", "🔎 Show"]
    assert "Create file" in msg["body"]["text"] and all(len(t) <= 20 for t in ids)
    a = core.bus.pending()[0]
    assert ids["✅ Approve"] == f"approve:{a.id}:{a.nonce}:{a.payload_hash[:8]}"
    post(http, payload(button(ids["✅ Approve"])))
    settle(core, hub)
    assert (root / "todo.txt").read_text() == "buy milk"
    assert core.bus.get(a.id).decided_by == "whatsapp"
    assert graph.texts()[-2:] == ["✅ Approved: " + a.summary[:120], "Done. Milk is on the list."]


def test_deny_and_show_buttons(tmp_path):
    core, hub, graph, http, root = make(tmp_path, [
        ([call("write_file", "c1", path="todo.txt", content="buy milk")], ""),
        ([], "Okay, I won't."),
    ])
    post(http, payload(text("add milk")))
    _, ids = card(graph)
    post(http, payload(button(ids["🔎 Show"], mid="wamid.s")))
    assert wait_for(lambda: any(t.startswith("🔎 write_file") for t in graph.texts()))
    details = [t for t in graph.texts() if t.startswith("🔎")][0]
    assert "sha256" in details and "buy milk" not in details      # hashes, not contents
    post(http, payload(button(ids["❌ Deny"], mid="wamid.d")))
    settle(core, hub)
    assert not (root / "todo.txt").exists() and "❌ Denied" in graph.texts()[-2]


def test_forged_or_stale_buttons_do_nothing(tmp_path):
    core, hub, graph, http, root = approval_flow(tmp_path)
    post(http, payload(text("add milk")))
    _, ids = card(graph)
    verb, aid, nonce, h8 = ids["✅ Approve"].split(":")
    post(http, payload(button(f"approve:{aid}:guessed-nonce:{h8}", mid="wamid.x1")))
    post(http, payload(button(f"approve:{aid}:{nonce}:deadbeef", mid="wamid.x2")))
    post(http, payload(button("approve:nope:nope:nope", mid="wamid.x3")))
    assert wait_for(lambda: sum("isn't open any more" in t for t in graph.texts()) == 3)
    assert core.bus.pending() and not (root / "todo.txt").exists()
    core.stop()
    settle(core, hub)


def test_stop_keyword_is_the_kill_switch(tmp_path):
    core, hub, graph, http, root = approval_flow(tmp_path)
    post(http, payload(text("add milk")))
    card(graph)
    post(http, payload(text("STOP!", mid="wamid.stop")))
    settle(core, hub)
    assert not (root / "todo.txt").exists()
    assert core.memory.recent_tasks()[0].status == "cancelled"
    assert "🛑 Stopped. I cancelled the running task and denied 1 pending approval." in graph.texts()


# ---- voice notes -------------------------------------------------------------------------
def test_voice_note_is_transcribed_with_keywords_and_audited(tmp_path):
    heard = {}

    def fake_transcribe(wav, keywords=None):
        heard["wav"], heard["keywords"] = wav, keywords
        return {"transcript": "Fill the hackathon reimbursement form", "audioDurationMs": 4200}

    def fake_convert(src, dst):
        dst.write_bytes(b"RIFF....WAVEfmt " + src.read_bytes())
        return dst
    core, hub, graph, http, _ = make(tmp_path, [([], "Starting on the form.")],
                                     transcriber=fake_transcribe, converter=fake_convert)
    core.memory.remember("manager", "Priya Natarajan")
    audio = {"from": OWNER, "id": "wamid.v", "timestamp": "1", "type": "audio",
             "audio": {"mime_type": "audio/ogg; codecs=opus", "sha256": "x", "id": "media-123", "voice": True}}
    post(http, payload(audio))
    settle(core, hub)
    (task,) = core.memory.recent_tasks()
    assert task.goal == "[whatsapp] Fill the hackathon reimbursement form"
    assert graph.texts()[0] == "🎙️ “Fill the hackathon reimbursement form”"
    assert "Lantern" in heard["keywords"] and "Priya Natarajan" in heard["keywords"]
    media_gets = [r for r in graph.requests if r["method"] == "GET"]
    assert [r["url"].split("/")[-1] for r in media_gets][0] == "media-123"
    assert all(r["headers"]["Authorization"] == "Bearer tok" for r in media_gets)
    entries = core.audit.entries()
    asr = [e for e in entries if e["endpoint"] == "asr.transcribe"][0]
    assert asr["model"] == models.VOICE and asr["bytes"] > 0
    voice_cost = [u for u in core.memory.usage_by_model() if u["model"] == models.VOICE][0]["cost_usd"]
    assert voice_cost == pytest.approx(models.audio_cost(4200))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_opus_voice_note_converts_to_16k_wav(tmp_path):
    ogg = tmp_path / "note.ogg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libopus", str(ogg)], check=True)
    got = {}

    def fake_transcribe(wav, keywords=None):
        import wave
        with wave.open(str(wav)) as w:
            got["format"] = (w.getnchannels(), w.getframerate(), w.getsampwidth())
        return {"transcript": "status", "audioDurationMs": 1000}
    core, hub, graph, http, _ = make(tmp_path, [], transcriber=fake_transcribe)
    graph.audio = ogg.read_bytes()
    audio = {"from": OWNER, "id": "wamid.v2", "type": "audio", "audio": {"id": "media-123", "voice": True}}
    post(http, payload(audio))
    settle(core, hub)
    assert got["format"] == (1, 16000, 2)
    assert any(t.startswith("Idle.") for t in graph.texts())   # "status" spoken -> status reply


def test_voice_failure_is_reported_not_crashed(tmp_path):
    def broken(wav, keywords=None):
        raise RuntimeError("asr down")
    core, hub, graph, http, _ = make(tmp_path, [], transcriber=broken, converter=lambda s, d: s)
    post(http, payload({"from": OWNER, "id": "wamid.v3", "type": "audio", "audio": {"id": "media-123"}}))
    settle(core, hub)
    assert "couldn't hear that voice note" in graph.texts()[0] and core.memory.recent_tasks() == []
    assert hub.errors


# ---- outgoing hygiene ---------------------------------------------------------------------
def test_outgoing_text_is_scrubbed_and_audited(tmp_path):
    core, hub, graph, http, _ = make(tmp_path, [([], "Found it: AKIAABCDEFGHIJKLMNOP in config.")])
    post(http, payload(text("find my aws key")))
    settle(core, hub)
    assert "AKIAABCDEFGHIJKLMNOP" not in json.dumps(graph.requests)
    assert "[REDACTED:aws_access_key]" in graph.texts()[-1]
    sends = [e for e in core.audit.entries() if e["endpoint"] == "whatsapp.send"]
    assert len(sends) == 2 and sends[0]["destination"] == "https://graph.facebook.com"
    assert core.audit.summary()["cloud"]["by_model"]["whatsapp"]["calls"] == 2


def test_help_reply_shows_menu(tmp_path):
    core, hub, graph, http, _ = make(tmp_path, [])
    post(http, payload(text("/help")))
    settle(core, hub)
    lists = [m for m in graph.sent("interactive") if m["interactive"]["type"] == "list"]
    assert lists and lists[0]["interactive"]["action"]["button"] == "Menu"
    rows = lists[0]["interactive"]["action"]["sections"][0]["rows"]
    assert [r["title"] for r in rows] == ["Status", "Reminders", "Undo last task", "Today's summary",
                                          "Stop everything"]
    assert core.memory.recent_tasks() == []


# ---- Telegram ------------------------------------------------------------------------------
class FakeTelegram:
    def __init__(self, updates):
        self.updates = list(updates)
        self.calls = []

    def __call__(self, method, url, headers, body):
        name = url.rsplit("/", 1)[-1]
        data = json.loads(body) if body else None
        self.calls.append((name, data))
        if name == "getUpdates":
            batch, self.updates = self.updates, []
            return 200, json.dumps({"ok": True, "result": batch}).encode()
        if name == "getFile":
            return 200, json.dumps({"ok": True, "result": {"file_path": "voice/file_1.oga"}}).encode()
        if "/file/bot" in url:
            return 200, b"OggS"
        return 200, json.dumps({"ok": True, "result": {}}).encode()

    def sent(self, name="sendMessage"):
        return [d for n, d in self.calls if n == name]


def test_telegram_text_buttons_and_callbacks(tmp_path):
    tg_http = FakeTelegram([
        {"update_id": 10, "message": {"message_id": 1, "chat": {"id": 4242}, "text": "add milk"}},
        {"update_id": 11, "message": {"message_id": 2, "chat": {"id": 999}, "text": "hi from a stranger"}},
    ])
    tg = TelegramChannel("BOT", "4242", http=tg_http, poll_timeout=0)
    core, hub, _, http, root = make(tmp_path, [
        ([call("write_file", "c1", path="todo.txt", content="buy milk")], ""),
        ([call("finish", "c2", status="done", summary="wrote todo.txt")], ""),
        ([], "Done."),
    ], channels=[tg])
    for item in tg.poll_once():
        hub.receive(item)
    assert tg.offset == 12
    assert wait_for(lambda: any("reply_markup" in m for m in tg_http.sent()))
    msg = [m for m in tg_http.sent() if "reply_markup" in m][0]
    row = msg["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in row] == ["✅ Approve", "❌ Deny", "🔎 Show"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in row)
    tg_http.updates = [{"update_id": 12, "callback_query": {"id": "cb1", "from": {"id": 4242},
                                                           "message": {"chat": {"id": 4242}},
                                                           "data": row[0]["callback_data"]}}]
    for item in tg.poll_once():
        hub.receive(item)
    settle(core, hub)
    assert (root / "todo.txt").read_text() == "buy milk" and hub.ignored == 1
    assert tg_http.sent("answerCallbackQuery") == [{"callback_query_id": "cb1", "text": "Approved"}]
    assert tg_http.sent()[-1] == {"chat_id": "4242", "text": "Done."}
    getupdates = [d for n, d in tg_http.calls if n == "getUpdates"]
    assert getupdates[1]["offset"] == 12 and getupdates[0]["allowed_updates"] == ["message", "callback_query"]


def test_telegram_poller_does_not_spin_on_instant_empty_answers():
    tg_http = FakeTelegram([])
    tg = TelegramChannel("BOT", "4242", http=tg_http, poll_timeout=0)
    tg.start(lambda item: None)
    time.sleep(0.3)
    tg.stop()
    assert len([n for n, _ in tg_http.calls if n == "getUpdates"]) <= 2


def test_telegram_voice_download():
    tg_http = FakeTelegram([{"update_id": 1, "message": {"message_id": 3, "chat": {"id": 4242},
                                                         "voice": {"file_id": "F1", "duration": 2}}}])
    tg = TelegramChannel("BOT", "4242", http=tg_http)
    (item,) = tg.poll_once()
    assert item.kind == "voice" and item.fetch_audio() == (b"OggS", ".oga")


# ---- simulated phone + config ------------------------------------------------------------------
def test_simulated_phone_routes(tmp_path):
    phone = FakeChannel()
    core, hub, _, http, _ = make(tmp_path, [([], "Hi! Send me a task.")], channels=[phone])
    assert http.get("/health").json()["channels"] == ["fake"]
    assert http.post("/channel/fake", json={"text": "hello"}).status_code == 401
    assert http.post("/channel/fake", json={"text": "hello"}, headers=H).json() == {"ok": True}
    settle(core, hub)
    msgs = http.get("/channel/messages", headers=H).json()["messages"]
    assert msgs[0] == {**msgs[0], "text": "hello", "from_owner": True}
    texts = [m["text"] for m in msgs]
    assert texts[-1] == "Hi! Send me a task." and texts[1].startswith("🏮 On it")
    assert http.get("/webhook/whatsapp").status_code == 404   # no WhatsApp configured


def test_channels_from_env():
    assert [c.name for c in channels_from_env({})] == ["fake"]
    assert channels_from_env({"LANTERN_CHANNELS": "none"}) == []
    env = {"LANTERN_CHANNELS": "whatsapp,telegram", "WHATSAPP_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "p",
           "LANTERN_WHATSAPP_OWNER": "91", "TELEGRAM_BOT_TOKEN": "b", "LANTERN_TELEGRAM_OWNER": "1"}
    assert [c.name for c in channels_from_env(env)] == ["whatsapp", "telegram"]
    with pytest.raises(ValueError):
        channels_from_env({"LANTERN_CHANNELS": "whatsapp"})
    with pytest.raises(ValueError):
        channels_from_env({"LANTERN_CHANNELS": "pigeon"})


def test_wav_voice_note_is_converted_not_overwritten(tmp_path):
    """Live run, Oct 27: a .wav voice note was converted onto itself and ffmpeg refused.
    (The API only takes mono s16 WAV at 16 or 24 kHz, so 22.05 kHz input must be converted.)"""
    from lantern import demo, voice
    src = tmp_path / "in.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=1",
                    "-ar", "22050", str(src)], check=True)
    got = {}

    def fake_transcribe(wav, keywords=None):
        import wave
        with wave.open(str(wav)) as w:
            got["format"] = (w.getnchannels(), w.getframerate(), w.getsampwidth())
        return {"transcript": "status", "audioDurationMs": 1000}
    cfg = demo.prepare(tmp_path / "data")
    core, hub, phone = demo.build(cfg, transcriber=fake_transcribe, converter=voice.to_wav16k)
    hub.start(listen=False)
    try:
        hub.receive(phone.incoming(audio=src.read_bytes(), ext=".wav"))
        hub.wait_idle(10)
        assert got["format"] == (1, 16000, 2)
        assert not any("couldn't hear" in m.get("text", "") for m in phone.messages())
    finally:
        core.shutdown()


# ---- agent packs on the phone (T3d: runs on laptop, phone or both) -----------------
PHONE_PACK = {"format": "lantern-agent/1", "name": "hello-pack", "title": "Hello",
              "description": "Says hi.", "author": "bea", "version": "1.0.0",
              "inputs": [{"key": "who", "question": "Who to greet?", "example": "Priya"}],
              "goal": "Say hello to {who}.", "instructions": "Just answer in one line.",
              "tools": ["list_folders"]}


def phone_core(tmp_path, script):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "a.db"), str(tmp_path / "z.json")
    cfg.agents_dir = str(tmp_path / "agents")
    return Core(cfg, FakeClient(script), approval_timeout=10, reminder_tick=0)


def phone_hub(core, *phones):
    hub = ChannelHub(core, list(phones)).start(listen=False)
    return hub


def phone_texts(phone):
    return [m["text"] for m in phone.messages() if m.get("type") == "text" and not m.get("from_owner")]


def phone_cards(phone):
    return [m for m in phone.messages() if m.get("type") == "buttons"]


def last_card_button(phone):
    assert wait_for(lambda: phone_cards(phone))
    card = phone_cards(phone)[-1]
    return card, {b["title"]: b["id"] for b in card["buttons"]}


def test_phone_lists_and_runs_a_phone_surface_pack(tmp_path):
    core = phone_core(tmp_path, [([call("finish", "c1", status="done", summary="hi Priya")], ""),
                                 ([], "hi Priya.")])
    phone = FakeChannel()
    hub = phone_hub(core, phone)
    try:
        core.packs.install(PHONE_PACK)
        core.set_pack_surface("hello-pack", "phone")
        hub.handle(FakeChannel.incoming(text="agents"))
        assert hub.wait_idle()
        card, ids = last_card_button(phone)
        assert "hello-pack" in card["text"] and ids.get("Run hello-pack") == "run:hello-pack"
        hub.handle(FakeChannel.incoming(button="run:hello-pack"))   # no inputs yet: it asks
        assert hub.wait_idle()
        assert "Who to greet?" in phone_texts(phone)[-1]
        assert "run hello-pack" in phone_texts(phone)[-1]
        hub.handle(FakeChannel.incoming(text="run hello-pack who=Priya"))
        assert hub.wait_idle() and core.wait_idle()
        (task,) = core.memory.recent_tasks()
        assert task.goal == "[agent:hello-pack] Say hello to Priya."
        assert "hi Priya." in phone_texts(phone)
    finally:
        hub.stop()
        core.shutdown()


def test_laptop_only_pack_is_refused_from_the_phone(tmp_path):
    from lantern.channels.hub import LAPTOP_ONLY
    core = phone_core(tmp_path, [])
    phone = FakeChannel()
    hub = phone_hub(core, phone)
    try:
        core.packs.install(PHONE_PACK)   # the default surface is laptop-only
        hub.handle(FakeChannel.incoming(text="agents"))
        assert hub.wait_idle()
        assert "No agents run on the phone" in phone_texts(phone)[-1]
        hub.handle(FakeChannel.incoming(text="run hello-pack who=Priya"))
        assert hub.wait_idle()
        assert LAPTOP_ONLY in phone_texts(phone)[-1]
        assert core.memory.recent_tasks() == []
    finally:
        hub.stop()
        core.shutdown()


def test_unknown_run_stays_an_ordinary_task(tmp_path):
    core = phone_core(tmp_path, [([], "on my way.")])
    phone = FakeChannel()
    hub = phone_hub(core, phone)
    try:
        hub.handle(FakeChannel.incoming(text="run errands quickly"))
        assert hub.wait_idle() and core.wait_idle()
        (task,) = core.memory.recent_tasks()
        assert task.goal == "[fake] run errands quickly"
    finally:
        hub.stop()
        core.shutdown()


def test_laptop_pack_cards_and_results_stay_off_the_phone(tmp_path):
    core = phone_core(tmp_path, [([call("write_file", "c1", path="n.txt", content="hi")], ""),
                                 ([call("finish", "c2", status="done", summary="wrote it")], ""),
                                 ([], "wrote it.")])
    phone = FakeChannel()
    hub = phone_hub(core, phone)
    try:
        core.packs.install({**PHONE_PACK, "tools": ["write_file"]})   # laptop-only
        tid = core.submit("", pack="hello-pack", inputs={"who": "Priya"})
        assert wait_for(lambda: core.bus.pending())   # the card exists on the bus ...
        hub.wait_idle()
        assert phone_cards(phone) == []               # ... but never reaches the phone
        a = core.bus.pending()[0]
        core.bus.decide(a.id, a.nonce, True, by="desktop")
        assert core.wait_idle() and hub.wait_idle()
        assert core.memory.get_task(tid).status == "done"
        assert phone_texts(phone) == []               # and neither does the result
    finally:
        hub.stop()
        core.shutdown()


def test_both_pack_cards_reach_both_channels_first_answer_wins(tmp_path):
    core = phone_core(tmp_path, [([call("write_file", "c1", path="n.txt", content="hi")], ""),
                                 ([call("finish", "c2", status="done", summary="wrote it")], ""),
                                 ([], "wrote it.")])
    first, second = FakeChannel(), FakeChannel()
    second.name = "phone2"
    hub = phone_hub(core, first, second)
    try:
        core.packs.install({**PHONE_PACK, "tools": ["write_file"]})
        core.set_pack_surface("hello-pack", "both")
        hub.handle(Incoming("fake", FAKE_OWNER, "text", text="run hello-pack who=Priya"))
        assert hub.wait_idle()
        _, ids1 = last_card_button(first)
        _, ids2 = last_card_button(second)
        assert ids1.get("✅ Approve") == ids2.get("✅ Approve")   # the same card on both
        aid = core.bus.pending()[0].id
        hub.handle(Incoming("phone2", FAKE_OWNER, "button", button=ids2["❌ Deny"]))
        assert hub.wait_idle()
        hub.handle(Incoming("fake", FAKE_OWNER, "button", button=ids1["✅ Approve"]))
        assert hub.wait_idle()
        assert [m["text"] for m in first.messages() if "isn't open" in m.get("text", "")]
        assert core.bus.get(aid).decided_by == "phone2"   # the first answer won
        assert core.wait_idle()
    finally:
        hub.stop()
        core.shutdown()
