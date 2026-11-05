"""T3e: signed agent packs (Ed25519, trust on first use, re-verified on every run)."""
import base64
import json
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient
from lantern import packsign
from lantern.agentpacks import PackError, validate
from lantern.config import Config
from lantern.core import Core
from lantern.server import create_app

FP = re.compile(r"^[0-9A-F]{4}-[0-9A-F]{4}$")


def good_pack(**over) -> dict:
    pack = {"format": "lantern-agent/1", "name": "tidy-test", "title": "Tidy test",
            "description": "Sorts a folder into subfolders.", "author": "achan", "version": "1.0.0",
            "inputs": [{"key": "folder", "question": "Which folder?", "example": "Downloads"}],
            "goal": "Tidy {folder} into subfolders.",
            "instructions": "List the folder, then call organize_files once with the whole plan.",
            "tools": ["list_folders", "list_dir", "organize_files"]}
    pack.update(over)
    return pack


def make_core(tmp_path, script=None, approval_timeout=30):
    root = Path(tmp_path) / "docs"
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path = str(tmp_path / "audit.db")
    cfg.zones_file = str(tmp_path / "zones.json")
    cfg.agents_dir = str(tmp_path / "agents")
    core = Core(cfg, FakeClient(script or []), approval_timeout=approval_timeout)
    return core, root


def pending(core, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        found = core.bus.pending()
        if found:
            return found[0]
        time.sleep(0.01)
    raise AssertionError("no approval was requested")


def install_in_thread(core, data, replace=False):
    out = {}
    t = threading.Thread(target=lambda: out.update(core.pack_install(data, replace=replace)))
    t.start()
    return t, out


def approve_first(core):
    a = pending(core)
    core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
    return a


def two_keys(tmp_path):
    home_a, home_b = Path(tmp_path) / "a", Path(tmp_path) / "b"
    priv_a, pub_a = packsign.ensure_key(home_a)
    priv_b, pub_b = packsign.ensure_key(home_b)
    assert pub_a != pub_b
    return (priv_a, pub_a), (priv_b, pub_b)


# ---- sign / verify ------------------------------------------------------------
def test_sign_then_verify_gives_signed(tmp_path):
    priv, pub = packsign.ensure_key(tmp_path)
    signed = packsign.sign(good_pack(), priv)
    assert packsign.verify(signed) == "signed"
    assert validate(signed).as_dict() == signed   # a signed pack still validates
    assert FP.match(packsign.fingerprint(pub))


def test_no_signature_gives_unsigned():
    assert packsign.verify(good_pack()) == "unsigned"
    assert validate(good_pack()).signature is None


def test_one_changed_character_gives_tampered_and_install_refuses_it(tmp_path):
    priv, _ = packsign.ensure_key(tmp_path)
    signed = packsign.sign(good_pack(), priv)
    tampered = dict(signed)
    last = tampered["instructions"]
    tampered["instructions"] = last[:-1] + ("!" if last[-1] != "!" else "?")
    assert packsign.verify(tampered) == "tampered"
    with pytest.raises(PackError, match="this pack was changed after it was signed"):
        validate(tampered)
    core, _ = make_core(tmp_path)
    try:
        with pytest.raises(PackError, match="changed after it was signed"):
            core.pack_install(tampered)
        assert core.bus.pending() == []   # a tampered pack asks for no approval
    finally:
        core.shutdown()


def test_forged_signature_is_flagged(tmp_path):
    (priv_a, pub_a), (priv_b, pub_b) = two_keys(tmp_path)
    signed = packsign.sign(good_pack(), priv_a)
    # Same content, someone else's public key in the seal: not this pack's signature.
    forged = {**signed, "signature": {**signed["signature"], "public_key": pub_b}}
    assert packsign.verify(forged) == "tampered"
    with pytest.raises(PackError, match="changed after it was signed"):
        validate(forged)
    # Same author name, a different key: the install card warns in red.
    core, _ = make_core(tmp_path)
    try:
        t, out = install_in_thread(core, signed)
        approve_first(core)
        t.join(timeout=10)
        assert out == {"installed": True, "verified": True, "name": "tidy-test"}
        other = packsign.sign(good_pack(name="tidy-b", title="Tidy B"), priv_b)
        t2, out2 = install_in_thread(core, other)
        a = approve_first(core)
        assert "different key" in a.summary
        t2.join(timeout=10)
        assert out2["installed"] is True
    finally:
        core.shutdown()


def test_bad_signature_shape_refused(tmp_path):
    priv, _ = packsign.ensure_key(tmp_path)
    signed = packsign.sign(good_pack(), priv)
    for bad in [{**signed["signature"], "alg": "rsa"},
                {**signed["signature"], "sig": "!!!"},
                {"alg": "ed25519"}]:
        with pytest.raises(PackError):
            validate({**signed, "signature": bad})


# ---- trust on first use -------------------------------------------------------
def test_trust_on_first_use_records_key_then_stays_quiet(tmp_path):
    (priv_a, pub_a), _ = two_keys(tmp_path)
    fp = packsign.fingerprint(pub_a)
    signed = packsign.sign(good_pack(), priv_a)
    core, _ = make_core(tmp_path)
    try:
        t, out = install_in_thread(core, signed)
        a = approve_first(core)
        assert "Trust it?" in a.summary and fp in a.summary
        t.join(timeout=10)
        assert out["installed"] is True
        trusted = json.loads((Path(tmp_path) / "agents" / "trusted_keys.json").read_text())
        assert trusted == {fp: "achan"}
        # Same key again: no "Trust it?" question, just the signed line.
        again = packsign.sign(good_pack(version="1.0.1"), priv_a)
        t2, out2 = install_in_thread(core, again, replace=True)
        a2 = approve_first(core)
        assert "Trust it?" not in a2.summary
        assert f"Signed by achan · key {fp}" in a2.summary
        t2.join(timeout=10)
        assert out2["installed"] is True
    finally:
        core.shutdown()


def test_unsigned_pack_still_installs_with_an_unsigned_card(tmp_path):
    core, _ = make_core(tmp_path)
    try:
        t, out = install_in_thread(core, good_pack())
        a = approve_first(core)
        assert "Unsigned: anyone could have changed it" in a.summary
        t.join(timeout=10)
        assert out == {"installed": True, "verified": True, "name": "tidy-test"}
        assert (Path(tmp_path) / "agents" / "trusted_keys.json").exists() is False
    finally:
        core.shutdown()


# ---- the key file -------------------------------------------------------------
def test_key_file_is_created_once_and_stays_owner_only(tmp_path):
    path = packsign.key_path(tmp_path)
    priv, pub = packsign.ensure_key(tmp_path)
    assert path.exists()
    before = (path.stat().st_mtime_ns, pub)
    again_priv, again_pub = packsign.ensure_key(tmp_path)
    assert (path.stat().st_mtime_ns, again_pub) == before   # created once, not rewritten
    assert again_priv.private_bytes_raw() == priv.private_bytes_raw()
    import os
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_private_key_never_appears_in_exported_packs(tmp_path):
    priv, pub = packsign.ensure_key(tmp_path)
    core, _ = make_core(tmp_path)
    try:
        t, out = install_in_thread(core, packsign.sign(good_pack(), priv))
        a = approve_first(core)
        t.join(timeout=10)
        assert out["installed"] is True
        raw = core.packs.export("tidy-test")
        private_b64 = base64.b64encode(priv.private_bytes_raw())
        for secret in (priv.private_bytes_raw(), private_b64, b"PRIVATE KEY"):
            assert secret not in raw   # only the public key and signature travel
        assert pub.encode() in raw   # the public half is the point of the seal
        assert packsign.verify(json.loads(raw)) == "signed"
        shown = json.dumps(a.summary).encode()
        assert private_b64 not in shown and b"PRIVATE KEY" not in shown
    finally:
        core.shutdown()


# ---- re-verify on every run ---------------------------------------------------
def test_run_reverifies_and_a_disk_edit_refuses_the_run(tmp_path):
    priv, _ = packsign.ensure_key(tmp_path)
    core, _ = make_core(tmp_path)
    try:
        t, out = install_in_thread(core, packsign.sign(good_pack(), priv))
        approve_first(core)
        t.join(timeout=10)
        assert out["installed"] is True
        path = Path(tmp_path) / "agents" / "tidy-test.json"
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        wrapper["pack"]["instructions"] += "!"
        path.write_text(json.dumps(wrapper, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="changed after it was signed"):
            core.submit("", pack="tidy-test", inputs={"folder": "Downloads"})
    finally:
        core.shutdown()


# ---- server routes --------------------------------------------------------------
TOKEN = "test-token"
H = {"X-Lantern-Token": TOKEN}


def make_http(tmp_path, script=None, approval_timeout=30):
    core, _ = make_core(tmp_path, script, approval_timeout)
    return core, TestClient(create_app(core, TOKEN))


def test_sign_route_signs_after_approval_and_export_carries_it(tmp_path):
    core, http = make_http(tmp_path)
    try:
        out = {}
        t = threading.Thread(target=lambda: out.update(
            http.post("/agents/install", json=good_pack(), headers=H).json()))
        t.start()
        end = time.time() + 10
        while time.time() < end:
            cards = http.get("/approvals", headers=H).json()
            if cards:
                break
            time.sleep(0.01)
        http.post(f"/approvals/{cards[0]['id']}", headers=H,
                  json={"nonce": cards[0]["nonce"], "approve": True,
                        "expected_hash": cards[0]["payload_hash"]}).json()
        t.join(timeout=15)
        assert out["installed"] is True
        assert http.post("/agents/tidy-test/sign").status_code == 401

        signed_out = {}
        t2 = threading.Thread(target=lambda: signed_out.update(
            http.post("/agents/tidy-test/sign", headers=H).json()))
        t2.start()
        end = time.time() + 10
        while time.time() < end:
            cards = http.get("/approvals", headers=H).json()
            if cards and cards[0]["action"] == "sign_agent":
                break
            time.sleep(0.01)
        assert cards[0]["action"] == "sign_agent" and FP.match(
            cards[0]["payload"]["fingerprint"])
        http.post(f"/approvals/{cards[0]['id']}", headers=H,
                  json={"nonce": cards[0]["nonce"], "approve": True,
                        "expected_hash": cards[0]["payload_hash"]}).json()
        t2.join(timeout=15)
        assert signed_out["signed"] is True and signed_out["verified"] is True
        export = http.get("/agents/tidy-test/export", headers=H)
        assert packsign.verify(json.loads(export.content)) == "signed"
        got = http.get("/agents/tidy-test", headers=H).json()
        assert got["signature_status"] == "signed"
        assert got["fingerprint"] == signed_out["fingerprint"]
    finally:
        core.shutdown()


def test_tampered_file_on_disk_answers_422_and_run_is_refused(tmp_path):
    priv, _ = packsign.ensure_key(tmp_path)
    core, http = make_http(tmp_path)
    try:
        t, out = install_in_thread(core, packsign.sign(good_pack(), priv))
        approve_first(core)
        t.join(timeout=10)
        assert out["installed"] is True
        path = Path(tmp_path) / "agents" / "tidy-test.json"
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        wrapper["pack"]["instructions"] += "!"
        path.write_text(json.dumps(wrapper, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        assert http.get("/agents/tidy-test", headers=H).status_code == 422
        r = http.post("/agents/tidy-test/run", headers=H, json={"inputs": {"folder": "x"}})
        assert r.status_code == 400 and "changed after it was signed" in r.text
    finally:
        core.shutdown()
