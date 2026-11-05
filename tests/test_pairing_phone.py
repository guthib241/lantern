"""Paired phone: pairing codes, device auth, LAN guard, WhatsApp-level privacy."""
import asyncio
import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from fakes import FakeClient
from lantern import phone as phone_view
from lantern.config import Config
from lantern.core import Core
from lantern.memory import PRIVATE_SUMMARY
from lantern.pairing import PairingError, PairingStore
from lantern.server import PHONE_DIR, client_is_local, create_app

TOKEN = "test-token"


class Clock:
    def __init__(self, now=1000000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_core(tmp_path, script=None):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path = str(tmp_path / "audit.db")
    cfg.zones_file = str(tmp_path / "zones.json")
    cfg.media_dir, cfg.profile_path = str(tmp_path / "media"), str(tmp_path / "profile.json")
    core = Core(cfg, FakeClient(script or []), approval_timeout=5)
    return core


def make_app(tmp_path, core=None, phone_mode=False, clock=None, codes=None):
    core = core or make_core(tmp_path)
    it = iter(codes or [])

    def code_gen():
        try:
            return next(it)
        except StopIteration:
            return "123456"

    store = PairingStore(tmp_path / "devices.json", clock=clock or time.time, code_gen=code_gen)
    app = create_app(core, TOKEN, pairing=store, phone_mode=phone_mode)
    return core, TestClient(app), store


H = {"X-Lantern-Token": TOKEN}


def device_header(tok):
    return {"X-Lantern-Device": tok}


def pair(client, code="123456", name="My phone"):
    return client.post("/pair", json={"code": code, "name": name})


# ---- pairing unit ------------------------------------------------------------
def test_pairing_single_use_and_hashes_only(tmp_path):
    clock = Clock()
    store = PairingStore(tmp_path / "d.json", clock=clock, code_gen=lambda: "111111")
    code, ttl = store.start()
    assert code == "111111" and ttl == 300
    tok, did = store.redeem("111111", "Pixel")
    assert tok and did
    try:
        store.redeem("111111", "Pixel")
        raise AssertionError("single use")
    except PairingError:
        pass
    raw = json.loads((tmp_path / "d.json").read_text())
    text = json.dumps(raw)
    assert tok not in text and "111111" not in text  # hashes only
    assert stat.S_IMODE(os.stat(tmp_path / "d.json").st_mode) == 0o600
    assert store.verify(tok)["id"] == did
    assert store.verify("nope") is None


def test_pairing_expiry(tmp_path):
    clock = Clock()
    store = PairingStore(tmp_path / "d.json", clock=clock, code_gen=lambda: "222222")
    store.start()
    clock.now += 301
    try:
        store.redeem("222222", "p")
        raise AssertionError("expired")
    except PairingError as e:
        assert "expired" in str(e)


def test_pairing_five_wrong_burns(tmp_path):
    clock = Clock()
    store = PairingStore(tmp_path / "d.json", clock=clock, code_gen=lambda: "333333")
    store.start()
    for _ in range(5):
        try:
            store.redeem("000000", "p")
        except PairingError:
            pass
    try:
        store.redeem("333333", "p")
        raise AssertionError("burned")
    except PairingError:
        pass


def test_pairing_new_start_replaces_old(tmp_path):
    clock = Clock()
    codes = iter(["444444", "555555"])
    store = PairingStore(tmp_path / "d.json", clock=clock, code_gen=lambda: next(codes))
    store.start()
    store.start()
    try:
        store.redeem("444444", "p")
        raise AssertionError("replaced")
    except PairingError:
        pass
    tok, _ = store.redeem("555555", "p")
    assert store.verify(tok)


# ---- server: pairing routes ----------------------------------------------------
def test_pair_start_needs_desktop_token(tmp_path):
    core, c, _ = make_app(tmp_path, codes=["123456"])
    assert c.post("/pair/start").status_code in (401, 422)
    r = c.post("/pair/start", headers=H).json()
    assert r["code"].isdigit() and len(r["code"]) == 6
    assert r["expires_in"] == 300 and r["url"] == "/m"


def test_pair_redeem_and_revoke_gives_401(tmp_path):
    core, c, _ = make_app(tmp_path, codes=["123456"])
    c.post("/pair/start", headers=H)
    r = pair(c).json()
    tok, did = r["device_token"], r["device_id"]
    assert c.get("/m/api/tasks", headers=device_header(tok)).status_code == 200
    assert pair(c).status_code in (400, 409)  # single use
    assert c.get("/devices", headers=H).json()["devices"]
    assert c.post(f"/devices/{did}/revoke", headers=H).json() == {"id": did, "revoked": True}
    assert c.get("/m/api/tasks", headers=device_header(tok)).status_code == 401


def test_device_auth_on_every_phone_route(tmp_path):
    core, c, _ = make_app(tmp_path, codes=["123456"])
    c.post("/pair/start", headers=H)
    tok = pair(c).json()["device_token"]
    bad = {"X-Lantern-Device": "wrong"}
    paths = [("/m/api/tasks", "get"), ("/m/api/approvals", "get"), ("/m/api/reminders", "get")]
    for path, _ in paths:
        assert c.get(path).status_code == 401
        assert c.get(path, headers=bad).status_code == 401
        assert c.get(path, headers=device_header(tok)).status_code == 200
    assert c.post("/m/api/task", json={"goal": "hi"}).status_code == 401
    assert c.post("/m/api/task", json={"goal": "hi"}, headers=bad).status_code == 401
    assert c.get("/m/api/tasks/9999", headers=device_header(tok)).status_code == 404
    assert c.get("/m/api/tasks/9999").status_code == 401
    assert c.post("/m/api/approvals/nope", json={"nonce": "x", "approve": True}).status_code == 401
    assert c.post("/m/api/reminders/1/cancel").status_code == 401
    assert c.post("/m/api/stop").status_code == 401
    assert c.post("/m/api/stop", headers=device_header(tok)).status_code == 200


# ---- LAN guard -----------------------------------------------------------------
def test_client_is_local_shapes():
    def req(host):
        return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": (host, 1)})
    assert client_is_local(req("127.0.0.1"))
    assert client_is_local(req("::1"))
    assert client_is_local(req("testclient"))
    assert not client_is_local(req("192.168.1.5"))
    forwarded = Request({"type": "http", "method": "GET", "path": "/", "client": ("127.0.0.1", 1),
                         "headers": [(b"x-forwarded-for", b"203.0.113.7")]})
    assert not client_is_local(forwarded)


def test_desktop_routes_refuse_nonlocal_when_phone_on(tmp_path, monkeypatch):
    import lantern.server as srv
    core, c, _ = make_app(tmp_path, phone_mode=True)
    assert c.get("/tasks", headers=H).status_code == 200  # local test client still fine
    monkeypatch.setattr(srv, "client_is_local", lambda request: False)
    try:
        assert c.get("/tasks", headers=H).status_code == 403
        assert c.post("/pair/start", headers=H).status_code == 403
        assert c.get("/devices", headers=H).status_code == 403
        assert c.get("/health").status_code == 200  # open
        assert c.get("/m/").status_code == 200  # phone shell stays open
        r = c.post("/pair", json={"code": "000000", "name": "p"})
        assert r.status_code in (400, 409)  # redeem stays reachable, fails on the code not the guard
    finally:
        monkeypatch.undo()


def test_tunnelled_requests_and_desktop_sockets_are_not_local_when_phone_on(tmp_path):
    """A tunnel (cloudflared, ngrok) reaches Lantern from 127.0.0.1 but adds forwarding headers: those requests are
    from elsewhere. The desktop's websockets (/events, /talk) stay on this computer too."""
    core, c, _ = make_app(tmp_path, phone_mode=True)
    tunnel = {**H, "X-Forwarded-For": "203.0.113.7"}
    assert c.get("/tasks", headers=tunnel).status_code == 403
    assert c.get("/tasks", headers={**H, "CF-Connecting-IP": "203.0.113.7"}).status_code == 403
    assert c.get("/m/").status_code == 200 and c.get("/m/", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 200
    token = H["X-Lantern-Token"]
    for path in ("/events", "/talk"):
        # refused before accept: the test client raises WebSocketDisconnect, or sometimes CancelledError (a
        # BaseException) as its task is torn down; either way the socket never opened
        with pytest.raises((Exception, asyncio.CancelledError)):
            with c.websocket_connect(f"{path}?token={token}", headers={"X-Forwarded-For": "203.0.113.7"}) as ws:
                ws.receive_json()
    with c.websocket_connect(f"/events?token={token}") as ws:   # the desktop itself still connects
        pass


def test_phone_mode_off_keeps_localhost_routes(tmp_path):
    core, c, _ = make_app(tmp_path, phone_mode=False)
    assert c.get("/tasks", headers=H).status_code == 200


# ---- privacy ----------------------------------------------------------------------
def test_private_reply_never_reaches_phone(tmp_path):
    core, c, _ = make_app(tmp_path, codes=["123456"])
    c.post("/pair/start", headers=H)
    tok = pair(c).json()["device_token"]
    secret = "balance SECRET-12345"
    t = core.memory.start_task("read the private file")
    core.memory.log_step(t.id, "reply", secret)
    core.memory.finish_task(t.id, "done", secret)
    core.memory.mark_private(t.id)
    tasks = c.get("/m/api/tasks", headers=device_header(tok)).json()
    got = [x for x in tasks if x["id"] == t.id][0]
    assert got["reply"] == PRIVATE_SUMMARY and secret not in json.dumps(tasks)
    one = c.get(f"/m/api/tasks/{t.id}", headers=device_header(tok)).json()
    assert one["reply"] == PRIVATE_SUMMARY and secret not in json.dumps(one)
    assert all(secret not in (s.get("text", "") or "") for s in one["steps"])


def test_phone_task_steps_folded_without_file_text(tmp_path):
    core, c, _ = make_app(tmp_path, codes=["123456"])
    c.post("/pair/start", headers=H)
    tok = pair(c).json()["device_token"]
    t = core.memory.start_task("tidy downloads")
    core.memory.log_step(t.id, "tool_call", {"name": "read_file", "args": {"path": "/Docs/secret.txt"}})
    core.memory.log_step(t.id, "tool_result", {"name": "read_file", "result": {"text": "SECRET-BODY"}})
    core.memory.log_step(t.id, "reply", "Done.")
    core.memory.finish_task(t.id, "done", "Done.")
    one = c.get(f"/m/api/tasks/{t.id}", headers=device_header(tok)).json()
    assert "SECRET-BODY" not in json.dumps(one) and "/Docs/secret.txt" not in json.dumps(one)


def test_phone_approval_same_nonce_hash_check(tmp_path):
    core, c, _ = make_app(tmp_path, codes=["123456"])
    c.post("/pair/start", headers=H)
    tok = pair(c).json()["device_token"]
    a = core.bus.request("write_file", "Create file todo.txt",
                         {"path": "/docs/todo.txt", "sha256": "abc", "chars": 8}, tier="yellow")
    items = c.get("/m/api/approvals", headers=device_header(tok)).json()
    assert len(items) == 1 and items[0]["nonce"] == a.nonce
    assert items[0]["payload_hash"] == a.payload_hash and "payload" not in items[0]
    assert "abc" not in items[0]["text"]  # hashes never shown, like the WhatsApp card
    bad = c.post(f"/m/api/approvals/{a.id}", headers=device_header(tok),
                 json={"nonce": "wrong", "approve": True})
    assert bad.status_code == 409
    changed = c.post(f"/m/api/approvals/{a.id}", headers=device_header(tok),
                     json={"nonce": a.nonce, "approve": True, "expected_hash": "0" * 64})
    assert changed.status_code == 409
    ok = c.post(f"/m/api/approvals/{a.id}", headers=device_header(tok),
                json={"nonce": a.nonce, "approve": True, "expected_hash": a.payload_hash})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"


def test_phone_view_helpers_match_whatsapp_card():
    from lantern.channels.cards import approval_card
    from lantern.approvals import ApprovalBus
    bus = ApprovalBus()
    a = bus.request("send_email", "Send email to priya@example.com",
                    {"from": "me@example.com", "to": ["priya@example.com"], "cc": [],
                     "subject": "Slides", "body_sha256": "abc", "body_chars": 12}, tier="red")
    card = approval_card(a, a.created_at + 60)
    view = phone_view.approval_view(a, a.created_at + 60)
    assert view["text"] == card.text and view["footer"] == card.footer
    assert "payload" not in view and view["nonce"] == a.nonce


# ---- static shell -------------------------------------------------------------------
def test_phone_shell_csp_and_no_inline_script(tmp_path):
    core, c, _ = make_app(tmp_path)
    r = c.get("/m")
    assert r.status_code == 200
    assert "Content-Security-Policy" in r.headers
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    body = r.text
    assert '<meta http-equiv="Content-Security-Policy"' in body
    assert '<script src="/m/app.js"' in body
    inline = [line for line in body.splitlines() if "<script" in line and "src=" not in line]
    assert inline == []
    assert PHONE_DIR.joinpath("sw.js").exists()
    sw = PHONE_DIR.joinpath("sw.js").read_text()
    assert "/m/api/" in sw  # shell cached, API never cached


def test_phone_events_reject_bad_device(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    core, c, _ = make_app(tmp_path, codes=["123456"])
    c.post("/pair/start", headers=H)
    tok = pair(c).json()["device_token"]
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/m/events?device=nope"):
            pass
    with c.websocket_connect(f"/m/events?device={tok}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and "pending" in hello
        assert all("payload" not in p for p in hello["pending"])
