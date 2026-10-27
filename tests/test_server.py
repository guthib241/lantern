"""M6: local API server: tasks, approvals over HTTP, audit, zones, websocket events with badges."""
import time

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call, usage
from lantern.config import Config
from lantern.core import Core
from lantern.server import create_app, load_token

TOKEN = "test-token"


def make(tmp_path, script):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path = str(tmp_path / "audit.db")
    cfg.zones_file = str(tmp_path / "zones.json")
    core = Core(cfg, FakeClient(script), approval_timeout=5)
    return core, TestClient(create_app(core, TOKEN)), root


H = {"X-Lantern-Token": TOKEN}


def test_health_is_open_everything_else_needs_token(tmp_path):
    core, c, _ = make(tmp_path, [])
    assert c.get("/health").json()["ok"]
    assert c.get("/tasks").status_code == 401
    assert c.get("/tasks", headers={"X-Lantern-Token": "nope"}).status_code == 401
    assert c.get("/tasks", headers=H).status_code == 200


def test_task_runs_and_reports_cost(tmp_path):
    core, c, _ = make(tmp_path, [([], "Hello there.", usage(1000, 0, 20))])
    tid = c.post("/task", json={"goal": "hi"}, headers=H).json()["task_id"]
    assert core.wait_idle()
    t = c.get(f"/tasks/{tid}", headers=H).json()
    assert t["status"] == "done" and t["usage"]["calls"] == 1 and t["usage"]["cost_usd"] > 0
    assert any(s["kind"] == "reply" for s in t["steps"])
    assert c.post("/task", json={"goal": "  "}, headers=H).status_code == 400


def test_approval_round_trip_over_http(tmp_path):
    core, c, root = make(tmp_path, [
        ([call("write_file", "c1", path="todo.txt", content="buy milk")], ""),
        ([call("finish", "c2", status="done", summary="wrote todo.txt")], ""),
        ([], "Done."),
    ])
    c.post("/task", json={"goal": "make a todo"}, headers=H)
    pending = []
    for _ in range(300):
        pending = c.get("/approvals", headers=H).json()
        if pending:
            break
        time.sleep(0.01)
    assert pending and pending[0]["action"] == "write_file"
    a = pending[0]
    assert c.post(f"/approvals/{a['id']}", json={"nonce": "wrong", "approve": True}, headers=H).status_code == 409
    r = c.post(f"/approvals/{a['id']}", json={"nonce": a["nonce"], "approve": True,
                                               "expected_hash": a["payload_hash"]}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert core.wait_idle()
    assert (root / "todo.txt").read_text() == "buy milk"


def test_stop_denies_pending_and_cancels(tmp_path):
    core, c, root = make(tmp_path, [
        ([call("write_file", "c1", path="x.txt", content="x")], ""),
        ([], "fine"),
    ])
    tid = c.post("/task", json={"goal": "write x"}, headers=H).json()["task_id"]
    for _ in range(300):
        if c.get("/approvals", headers=H).json():
            break
        time.sleep(0.01)
    assert c.post("/stop", headers=H).json()["denied_approvals"] == 1
    assert core.wait_idle()
    assert not (root / "x.txt").exists()
    assert c.get(f"/tasks/{tid}", headers=H).json()["status"] == "cancelled"


def test_websocket_streams_events_with_badges(tmp_path):
    core, c, _ = make(tmp_path, [
        ([call("list_dir", "c1")], "", usage(500, 0, 10)),
        ([], "Your folder is empty.", usage(600, 400, 10)),
    ])
    with c.websocket_connect(f"/events?token={TOKEN}") as ws:
        assert ws.receive_json()["type"] == "hello"
        c.post("/task", json={"goal": "list"}, headers=H)
        seen = []
        while True:
            e = ws.receive_json()
            seen.append(e)
            if e["type"] == "done":
                break
    kinds = [e["type"] for e in seen]
    assert kinds[0] == "queued" and "task" in kinds and "tool" in kinds
    assert [e["badge"] for e in seen if e["type"] == "usage"] == ["Muse Spark", "Muse Spark"]
    assert [e["badge"] for e in seen if e["type"] == "tool"] == ["on this computer"]
    assert all(e.get("task_id") for e in seen if e["type"] in ("tool", "usage"))


def test_websocket_rejects_bad_token(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    core, c, _ = make(tmp_path, [])
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/events?token=bad") as ws:
            ws.receive_json()


def test_audit_zones_memory_usage_routes(tmp_path):
    core, c, root = make(tmp_path, [([], "ok", usage(10, 0, 1))])
    c.post("/task", json={"goal": "hello"}, headers=H)
    core.wait_idle()
    a = c.get("/audit", headers=H).json()
    assert a["summary"]["cloud"]["calls"] == 1 and a["entries"][0]["sha256"]
    private = root / "Private"
    private.mkdir()
    z = c.post("/zones", json={"path": str(private), "zone": "private"}, headers=H).json()
    assert {"path": str(private.resolve()), "zone": "private"} in z["zones"]
    assert c.post("/zones", json={"path": str(private), "zone": "bogus"}, headers=H).status_code == 400
    assert c.get("/memory", headers=H).json() == {"facts": {}}
    assert c.get("/usage", headers=H).json()["total_usd"] > 0


def test_no_key_still_starts_and_fails_task_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    root = tmp_path / "d"
    root.mkdir()
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "a.db"), str(tmp_path / "z.json")
    core = Core(cfg)
    c = TestClient(create_app(core, TOKEN))
    assert c.get("/health").json()["has_key"] is False
    tid = c.post("/task", json={"goal": "hi"}, headers=H).json()["task_id"]
    core.wait_idle()
    assert c.get(f"/tasks/{tid}", headers=H).json()["status"] == "failed"


def test_token_file_created_private(tmp_path):
    import os
    t = load_token(tmp_path)
    assert t == load_token(tmp_path) and oct(os.stat(tmp_path / "token").st_mode)[-3:] == "600"


def test_real_uvicorn_serves_the_event_websocket(tmp_path):
    """The desktop app talks to a real uvicorn, not the test client: /events must upgrade there too."""
    import socket
    import threading

    import uvicorn
    from websockets.sync.client import connect

    core, _, _ = make(tmp_path, [])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(core, TOKEN), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        with connect(f"ws://127.0.0.1:{port}/events?token={TOKEN}", origin="null") as ws:
            import json
            assert json.loads(ws.recv(timeout=5))["type"] == "hello"
            assert len(core._sinks) == 1
        for _ in range(200):  # the window closed: its sink goes away without waiting for an event
            if not core._sinks:
                break
            time.sleep(0.02)
        assert core._sinks == []
    finally:
        server.should_exit = True
        thread.join(5)
    assert not thread.is_alive()  # shutdown isn't stuck on a forgotten websocket


def test_zones_say_whether_each_path_exists(tmp_path):
    core, c, root = make(tmp_path, [])
    zones = c.get("/zones", headers=H).json()["zones"]
    by_path = {z["path"]: z["exists"] for z in zones}
    assert by_path[str(root.resolve())] is True
    assert any(z["zone"] == "hidden" and z["exists"] is False for z in zones)   # other systems' defaults


def test_settings_change_the_persona_and_are_remembered(tmp_path):
    import json as _json
    core, c, _ = make(tmp_path, [])
    assert c.get("/settings", headers=H).json() == {"persona": "smug", "personas": ["helpful", "sarcastic", "smug"]}
    assert c.post("/settings", json={"persona": "helpful"}, headers=H).json()["persona"] == "helpful"
    assert core.agent.persona == "helpful" and "warm, upbeat" in core.agent.instructions()
    saved = _json.loads((tmp_path / "config.json").read_text())   # next to memory.db, not the real ~/.lantern
    assert saved == {"persona": "helpful"}
    assert c.post("/settings", json={"persona": "rude"}, headers=H).status_code == 400
    assert c.post("/settings", json={"persona": "smug"}).status_code == 401
    from lantern import config as config_mod
    import pytest as _pytest
    with _pytest.raises(ValueError):
        config_mod.save_setting("roots", ["/"], tmp_path)   # only allowlisted keys
