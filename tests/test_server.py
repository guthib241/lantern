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
