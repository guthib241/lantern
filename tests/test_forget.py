"""M15: memory + audit encrypted at rest; forgetting really removes the bytes."""
import os
import stat

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call
from lantern import crypto
from lantern.audit import AuditLog
from lantern.config import Config
from lantern.core import Core
from lantern.crypto import Cipher, CryptoError, load_cipher
from lantern.memory import Memory
from lantern.policy import Policy, PolicyError
from lantern.server import create_app
from cryptography.fernet import Fernet


def raw(*paths):
    return b"".join(p.read_bytes() for p in paths if p.exists())


def make_core(tmp_path, script):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    cfg = Config(roots=[str(docs)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "a.db"), str(tmp_path / "zones.json")
    cfg.media_dir, cfg.browser_profile_dir = str(tmp_path / "media"), str(tmp_path / "browser-profile")
    cfg.profile_path, cfg.envmap_path, cfg.widgets_dir = str(tmp_path / "profile.json"), str(tmp_path / "e.db"), str(tmp_path / "w")
    return Core(cfg, FakeClient(script), approval_timeout=5), cfg


def test_nothing_readable_in_the_database_files(tmp_path):
    core, cfg = make_core(tmp_path, [
        ([call("remember", "c1", key="locker code hint", value="PINEAPPLE-88"),
          call("search_files", "c2", query="ZEPHYR-12")], ""),
        ([call("finish", "c3", status="done", summary="noted MARMALADE")], ""),
        ([], "Done: QUARTZ-55"),
    ])
    tid = core.submit("remember MARMALADE-77 for me")
    assert core.wait_idle()
    blob = raw(tmp_path / "m.db", tmp_path / "a.db")
    for marker in (b"MARMALADE", b"PINEAPPLE-88", b"ZEPHYR-12", b"QUARTZ-55"):
        assert marker not in blob, marker
    # ...while Lantern itself reads everything back normally
    assert core.memory.get_task(tid).goal == "remember MARMALADE-77 for me"
    assert core.memory.facts()["locker code hint"] == "PINEAPPLE-88"
    assert any("QUARTZ-55" in s["content"] for s in core.memory.steps(tid))
    assert "MARMALADE-77" in core.audit.entries()[-1]["preview"]
    assert stat.S_IMODE(os.stat(tmp_path / "key").st_mode) == 0o600
    core.shutdown()


def test_old_plaintext_rows_are_encrypted_on_first_open(tmp_path):
    db = tmp_path / "m.db"
    mem = Memory(db)
    t = mem.start_task("OLDPLAIN goal")
    mem.log_step(t.id, "note", "OLDPLAIN step")
    mem.remember("city", "OLDPLAIN-CITY")
    mem.close()
    assert b"OLDPLAIN" in db.read_bytes()
    enc = Memory(db, load_cipher(tmp_path))
    assert b"OLDPLAIN" not in db.read_bytes()
    assert enc.get_task(t.id).goal == "OLDPLAIN goal" and enc.facts() == {"city": "OLDPLAIN-CITY"}


def test_wrong_key_cannot_read(tmp_path):
    mem = Memory(tmp_path / "m.db", Cipher(Fernet.generate_key()))
    t = mem.start_task("secret plan")
    mem.close()
    with pytest.raises(CryptoError):
        Memory(tmp_path / "m.db", Cipher(Fernet.generate_key())).get_task(t.id)


def test_forget_really_removes_bytes(tmp_path):
    # Plaintext on purpose: proves secure_delete + VACUUM take the bytes out of the file.
    db, adb = tmp_path / "m.db", tmp_path / "a.db"
    mem, audit = Memory(db), AuditLog(adb)
    keep = mem.start_task("KEEP-ME")
    gone = mem.start_task("GHOST-99 task")
    mem.log_step(gone.id, "reply", "GHOST-99 reply")
    mem.log_usage(gone.id, "muse-spark-1.3", 10, 0, 5, 0.001)
    mem.remember("door", "GHOST-FACT")
    audit.record(lane="cloud", destination="x", endpoint="responses", model="m", payload=b"x", task_id=gone.id,
                 preview="GHOST-99 preview")
    assert b"GHOST-99" in raw(db, adb)
    assert mem.forget_task(gone.id) == {"steps": 1, "usage": 1, "tasks": 1}
    assert audit.forget_task(gone.id) == 1
    assert mem.forget("door")
    assert b"GHOST" not in raw(db, adb)
    assert mem.get_task(keep.id).goal == "KEEP-ME" and mem.task_usage(gone.id)["calls"] == 0


def test_forget_everything(tmp_path):
    core, cfg = make_core(tmp_path, [([], "hi")])
    core.submit("hello")
    core.wait_idle()
    (tmp_path / "media" / "outbox").mkdir(parents=True)
    (tmp_path / "media" / "whatsapp-voice-1.ogg").write_bytes(b"OggS voice")
    (tmp_path / "media" / "outbox" / "mail.eml").write_text("Subject: hi")
    (tmp_path / "browser-profile" / "Default").mkdir(parents=True)
    (tmp_path / "browser-profile" / "Default" / "Cookies").write_bytes(b"session")
    (tmp_path / "profile.json").write_text('{"profile": {"full name": "Asha"}}')
    (tmp_path / "docs" / "mine.txt").write_text("user file stays")
    (tmp_path / "zones.json").write_text('{"zones": []}')
    r = core.forget_everything()
    assert r["memory"]["tasks"] == 1 and r["audit"] >= 1 and not r["skipped"]
    assert core.memory.recent_tasks() == [] and core.audit.entries() == []
    assert not (tmp_path / "media").exists() and not (tmp_path / "browser-profile").exists()
    assert not (tmp_path / "profile.json").exists()
    assert (tmp_path / "docs" / "mine.txt").exists() and (tmp_path / "key").exists() and (tmp_path / "zones.json").exists()
    core.shutdown()


def test_erase_only_touches_lanterns_own_data(tmp_path):
    home = tmp_path / "lantern-home"
    (home / "media").mkdir(parents=True)
    user = tmp_path / "docs"
    user.mkdir()
    (user / "a.txt").write_text("x")
    policy = Policy.from_roots([user])
    for target in (user, user / "a.txt", home, tmp_path / "elsewhere"):
        with pytest.raises(PolicyError):
            policy.erase_lantern_data(target, home)
    assert (user / "a.txt").exists()
    assert policy.erase_lantern_data(home / "media", home) == 0


def test_forget_routes(tmp_path):
    core, cfg = make_core(tmp_path, [([], "hi")])
    tid = core.submit("hello")
    core.wait_idle()
    http, h = TestClient(create_app(core, "t")), {"X-Lantern-Token": "t"}
    assert http.post("/forget/everything", json={"confirm": "yes"}, headers=h).status_code == 400
    assert http.post("/forget/task/999", headers=h).status_code == 404
    assert http.post(f"/forget/task/{tid}", headers=h).json()["tasks"] == 1
    core.memory.remember("pet", "Momo")
    assert http.post("/forget/fact", json={"key": "pet"}, headers=h).json() == {"forgot": "pet", "existed": True}
    assert http.post("/forget/everything", json={"confirm": "Forget everything"}, headers=h).status_code == 200
    core.shutdown()


def test_key_from_keychain_when_available(tmp_path, monkeypatch):
    monkeypatch.setenv("LANTERN_KEY_STORE", "auto")
    key = Fernet.generate_key()
    monkeypatch.setattr(crypto, "_from_keyring", lambda: key)
    c = load_cipher(tmp_path)
    assert c.where == "keychain" and not (tmp_path / "key").exists()
    monkeypatch.setattr(crypto, "_from_keyring", lambda: None)   # no keychain: fall back to the file
    assert load_cipher(tmp_path).where.endswith("key") and (tmp_path / "key").exists()
    with pytest.raises(CryptoError):
        monkeypatch.setenv("LANTERN_KEY_STORE", "keyring")
        load_cipher(tmp_path / "other")


def test_encryption_can_be_turned_off(tmp_path):
    cfg = Config(roots=[str(tmp_path)], db_path=str(tmp_path / "m.db"))
    cfg.encrypt = False
    assert crypto.cipher_for(cfg) is None
