"""T4c: agent faces — an emoji or a drawn icon per agent, shown while it runs."""
import base64
import threading
import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call
from lantern import models
from lantern.agentpacks import validate_emoji
from lantern.config import Config
from lantern.core import Core
from lantern.server import create_app

TOKEN = "test-token"
H = {"X-Lantern-Token": TOKEN}
WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00/\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00"

TINY = {"format": "lantern-agent/1", "name": "hello-pack", "title": "Hello", "description": "Says hi.",
        "author": "bea", "version": "1.0.0",
        "inputs": [{"key": "who", "question": "Who to greet?", "example": "Priya"}],
        "goal": "Say hello to {who}.", "instructions": "Just answer in one line.",
        "tools": ["list_folders"]}


def image_turn(n=1):
    return ([NS(type="image_generation_call", id=f"ig{n}", status="completed",
                result=base64.b64encode(WEBP).decode())], "")


def finish_script(summary, reply="done."):
    return [([call("finish", "c1", status="done", summary=summary)], ""), ([], reply)]


def make_core(tmp_path, script):
    root = tmp_path / "docs"
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path = str(tmp_path / "audit.db")
    cfg.zones_file = str(tmp_path / "zones.json")
    cfg.agents_dir = str(tmp_path / "agents")
    core = Core(cfg, FakeClient(script), approval_timeout=30)
    return core, TestClient(create_app(core, TOKEN))


def wait_approval(core, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        found = core.bus.pending()
        if found:
            return found[0]
        time.sleep(0.01)
    raise AssertionError("no approval was requested")


# ---- emoji faces ------------------------------------------------------------
def test_emoji_face_is_free_and_listed(tmp_path):
    core, c = make_core(tmp_path, [])
    try:
        core.packs.install(TINY)
        r = c.post("/agents/hello-pack/face", json={"emoji": "📦"}, headers=H)
        assert r.status_code == 200
        assert r.json() == {"made": True, "face": {"emoji": "📦"}, "cost_usd": 0.0, "model_calls": 0}
        assert c.get("/agents", headers=H).json()["agents"][0]["face"] == {"emoji": "📦"}
        assert c.get("/agents/hello-pack", headers=H).json()["face"] == {"emoji": "📦"}
        assert core.packs.face("hello-pack") == {"emoji": "📦"}
        assert c.get("/agents/hello-pack/face", headers=H).status_code == 404  # no icon drawn
        assert core.packs.export("hello-pack").find(b"face") == -1  # sharing stays data-only
    finally:
        core.shutdown()


def test_emoji_validation_and_routing(tmp_path):
    core, c = make_core(tmp_path, [])
    try:
        core.packs.install(TINY)
        assert c.post("/agents/hello-pack/face", json={"emoji": ""}, headers=H).status_code == 400
        assert c.post("/agents/hello-pack/face", json={"emoji": "x" * 9}, headers=H).status_code == 400
        assert c.post("/agents/hello-pack/face", json={}, headers=H).status_code == 400
        assert c.post("/agents/hello-pack/face",
                       json={"emoji": "📦", "generate": True}, headers=H).status_code == 400
        assert c.post("/agents/nope/face", json={"emoji": "📦"}, headers=H).status_code == 404
        assert c.post("/agents/BAD!/face", json={"emoji": "📦"}, headers=H).status_code == 404
        assert c.get("/agents/hello-pack/face").status_code == 401
        assert c.post("/agents/hello-pack/face", json={"emoji": "📦"}).status_code == 401
        assert core.packs.face("hello-pack") is None  # refused writes change nothing
    finally:
        core.shutdown()


def test_validate_emoji_rejects_blank_and_secrets():
    assert validate_emoji("📦") == "📦"
    with pytest.raises(Exception, match="empty"):
        validate_emoji("   ")
    with pytest.raises(Exception, match="at most"):
        validate_emoji("x" * 9)


# ---- drawn icons ------------------------------------------------------------
def test_generate_needs_an_approval_showing_the_cost(tmp_path):
    core, c = make_core(tmp_path, [image_turn(1)])
    try:
        core.packs.install(TINY)
        out = {}
        worker = threading.Thread(target=lambda: out.update(core.make_agent_face("hello-pack", generate=True)))
        worker.start()
        try:
            a = wait_approval(core)
            assert a.action == "agent_face"
            assert "$0.01" in a.summary and "Hello" in a.summary
            assert a.payload == {"name": "hello-pack", "images": 1, "cost_usd": 0.01}
            core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
            worker.join(timeout=30)
            assert out == {"made": True, "verified": True, "face": {"icon": True},
                           "cost_usd": 0.01, "model_calls": 1}
        finally:
            worker.join(timeout=30)
        icon = core.packs.dir / "hello-pack.webp"
        assert icon.read_bytes() == WEBP
        assert c.get("/agents/hello-pack/face", headers=H).content == WEBP
        assert c.get("/agents/hello-pack/face", headers=H).headers["content-type"] == "image/webp"
        assert c.get("/agents", headers=H).json()["agents"][0]["face"] == {"icon": True}
        usage = core.memory.usage_by_model()
        assert sum(u["cost_usd"] for u in usage if u["model"] == models.IMAGE) == pytest.approx(0.01)
    finally:
        core.shutdown()


def test_declining_draw_writes_nothing(tmp_path):
    core, c = make_core(tmp_path, [])
    try:
        core.packs.install(TINY)
        worker = threading.Thread(
            target=lambda: core.make_agent_face("hello-pack", generate=True))
        worker.start()
        try:
            a = wait_approval(core)
            core.bus.decide(a.id, a.nonce, False, by="test")
            worker.join(timeout=30)
        finally:
            worker.join(timeout=30)
        assert not (core.packs.dir / "hello-pack.webp").exists()
        assert core.packs.face("hello-pack") is None
        assert c.get("/agents/hello-pack/face", headers=H).status_code == 404
    finally:
        core.shutdown()


def test_face_file_refuses_bad_names(tmp_path):
    core, c = make_core(tmp_path, [image_turn(1)])
    try:
        core.packs.install(TINY)
        worker = threading.Thread(
            target=lambda: core.make_agent_face("hello-pack", generate=True))
        worker.start()
        a = wait_approval(core)
        core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
        worker.join(timeout=30)
        assert c.get("/agents/hello-pack/face").status_code == 401
        for bad in ("/agents/nope/face", "/agents/BAD!/face", "/agents/hello-pack/face/extra"):
            assert c.get(bad, headers=H).status_code == 404, bad
        assert c.get("/agents/%2E%2E/face", headers=H).status_code == 404
    finally:
        core.shutdown()


def test_queued_carries_the_pack_name(tmp_path):
    core, _ = make_core(tmp_path, finish_script("hi"))
    try:
        core.packs.install(TINY)
        seen = []
        off = core.subscribe(seen.append)
        try:
            plain = core.submit("say hi")
            pack_id = core.submit("", pack="hello-pack", inputs={"who": "Priya"})
        finally:
            off()
        by_task = {e["task_id"]: e for e in seen if e.get("type") == "queued"}
        assert by_task[plain]["pack"] is None
        assert by_task[pack_id]["pack"] == "hello-pack"
        assert core.wait_idle()
    finally:
        core.shutdown()
