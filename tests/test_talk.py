"""S15 live talk: the panel streams microphone PCM to Lantern's /talk; Lantern relays it to the realtime endpoint
(here a local fake that checks the documented handshake) and relays the transcript back. The key never leaves
Lantern's side; audio time is logged as Muse Voice Transcribe."""
import asyncio
import json
import threading

import pytest

websockets = pytest.importorskip("websockets")
from fastapi.testclient import TestClient  # noqa: E402

from lantern import models, talk  # noqa: E402
from lantern.core import Core  # noqa: E402
from lantern.server import create_app  # noqa: E402
from test_scheduler import config  # noqa: E402


class FakeRealtime:
    """wss://…/asr/realtime as the docs describe it: handshake first, ack, binary audio, endStream, then events."""

    def __init__(self, refuse: str = ""):
        self.refuse, self.handshakes, self.audio = refuse, [], 0
        self.ready = threading.Event()

    async def handler(self, ws):
        hs = json.loads(await ws.recv())
        self.handshakes.append(hs)
        if self.refuse:
            await ws.send(json.dumps({"type": "error", "message": self.refuse}))
            await ws.close(1008)
            return
        await ws.send(json.dumps({"sessionId": "s-1"}))
        async for m in ws:
            if isinstance(m, bytes):
                self.audio += len(m)
                if self.audio >= 48_000:   # a second in: a partial, as the service sends them while you speak
                    await ws.send(json.dumps({"type": "transcript", "transcript": "remind me", "final": False}))
            elif json.loads(m).get("type") == "endStream":
                await ws.send(json.dumps({"type": "audioProgress", "audioProcessedMs": 2000}))
                await ws.send(json.dumps({"type": "transcript", "transcript": "Remind me at 6 to call Ravi.",
                                          "final": True}))
                await ws.close(1000)
                return

    def start(self):
        def run():
            async def main():
                async with websockets.serve(self.handler, "127.0.0.1", 0) as server:
                    self.port = server.sockets[0].getsockname()[1]
                    self.ready.set()
                    await asyncio.Future()
            asyncio.run(main())
        threading.Thread(target=run, daemon=True).start()
        assert self.ready.wait(10)
        return f"ws://127.0.0.1:{self.port}"


def test_push_to_talk_is_relayed_and_the_key_stays_on_lanterns_side(tmp_path, monkeypatch):
    fake = FakeRealtime()
    monkeypatch.setenv("LANTERN_TALK_URL", fake.start())
    monkeypatch.setenv("MODEL_API_KEY", "sk-test-key")
    core = Core(config(tmp_path), reminder_tick=0)
    events = []
    core.subscribe(events.append)
    c = TestClient(create_app(core, "tok"))
    with c.websocket_connect("/talk?token=tok") as ws:
        for _ in range(5):
            ws.send_bytes(b"\x00\x01" * 12_000)       # 5 × 0.5 s of 24 kHz 16-bit mono
        ws.send_text(json.dumps({"type": "end"}))
        got = []
        while not got or got[-1]["type"] not in ("done", "error"):
            got.append(ws.receive_json())
    assert [g["type"] for g in got] == ["partial", "partial", "partial", "partial", "final", "done"]
    assert got[-1] == {"type": "done", "text": "Remind me at 6 to call Ravi.", "audio_ms": 2500}
    [hs] = fake.handshakes
    assert hs == {"mode": "PUSH_TO_TALK", "model": models.VOICE, "audioEncoding": "PCM_24KHZ",
                  "partialMode": "CUMULATIVE", "emitAudioProgress": False,
                  "authorization": {"accessToken": "Bearer sk-test-key"}}
    assert fake.audio == 120_000
    assert core.memory.total_cost() == pytest.approx(models.audio_cost(2500))   # 2 whole seconds
    flags = [e["on"] for e in events if e["type"] == "listening"]
    assert flags == [True, False]                                              # the avatar listens, then stops
    core.shutdown()


def test_refusals_bad_tokens_and_a_missing_key_are_said_plainly(tmp_path, monkeypatch):
    fake = FakeRealtime(refuse="invalid credential")
    monkeypatch.setenv("LANTERN_TALK_URL", fake.start())
    monkeypatch.setenv("MODEL_API_KEY", "sk-test-key")
    monkeypatch.setenv("LANTERN_TALK_TOKEN", "bare")
    core = Core(config(tmp_path), reminder_tick=0)
    c = TestClient(create_app(core, "tok"))
    with c.websocket_connect("/talk?token=tok") as ws:
        ws.send_text(json.dumps({"type": "end"}))
        msg = ws.receive_json()
    assert msg["type"] == "error" and "refused: invalid credential" in msg["text"]
    assert fake.handshakes[0]["authorization"] == {"accessToken": "sk-test-key"}   # the cookbook's form
    monkeypatch.delenv("MODEL_API_KEY")
    with c.websocket_connect("/talk?token=tok") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error" and "MODEL_API_KEY" in msg["text"]
    with pytest.raises(Exception):
        with c.websocket_connect("/talk?token=nope") as ws:
            ws.receive_json()
    core.shutdown()


def test_server_frames_become_partials_and_finals_and_the_rest_is_ignored():
    assert talk.event_of('{"sessionId": "x"}') is None                              # the ack has no type
    assert talk.event_of('{"type": "transcript", "transcript": " hi  there ", "final": false}') == \
        {"type": "partial", "text": "hi there"}
    assert talk.event_of('{"type": "transcript", "transcript": "Hi there.", "final": true}')["type"] == "final"
    assert talk.event_of('{"type": "speechStart", "turnId": 1}') is None
    assert talk.event_of(b"\x00") is None and talk.event_of("not json") is None
    assert talk.event_of('{"type": "error", "message": "backlog"}') == {"type": "error", "text": "backlog"}
    assert talk.handshake("k", keywords=["Lantern"])["keywords"] == ["Lantern"]
