"""T4a: avatar studio — design Lantern's face in words (Muse Image preview + full sheet)."""
import base64
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient
from lantern import models
from lantern.avatar import EXPRESSIONS
from lantern.config import Config
from lantern.core import Core
from lantern.server import create_app
from lantern import studio as studio_mod
from lantern.studio import StudioError

TOKEN = "test-token"
H = {"X-Lantern-Token": TOKEN}
WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00/\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00"


def image_turn(n=1):
    return ([NS(type="image_generation_call", id=f"ig{n}", status="completed",
                result=base64.b64encode(WEBP).decode())], "")


def make_core(tmp_path, script, approval_timeout=30):
    root = tmp_path / "docs"
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path = str(tmp_path / "audit.db")
    cfg.zones_file = str(tmp_path / "zones.json")
    fake = FakeClient(script)
    core = Core(cfg, fake, approval_timeout=approval_timeout)
    return core, TestClient(create_app(core, TOKEN)), fake


def pending(core, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        found = core.bus.pending()
        if found:
            return found[0]
        time.sleep(0.01)
    raise AssertionError("no approval was requested")


def accept_in_thread(core, design_id):
    out = {}
    t = threading.Thread(target=lambda: out.update(core.accept_avatar(design_id)))
    t.start()
    return t, out


# ---- brief validation -------------------------------------------------------
def test_brief_must_be_plain_text_within_600_chars():
    assert studio_mod.validate_brief("a tiny brass lantern robot") == "a tiny brass lantern robot"
    with pytest.raises(StudioError, match="few words"):
        studio_mod.validate_brief("   ")
    with pytest.raises(StudioError, match="few words"):
        studio_mod.validate_brief(None)
    with pytest.raises(StudioError, match="at most 600"):
        studio_mod.validate_brief("x" * 601)
    with pytest.raises(StudioError, match="style"):
        studio_mod.validate_style("watercolor")
    assert studio_mod.validate_style("neon") == "neon"
    assert "neon" in studio_mod.design_brief("a robot", "neon")


def test_bad_brief_and_style_are_400_with_no_model_call(tmp_path):
    core, c, fake = make_core(tmp_path, [])
    try:
        assert c.post("/avatar/design", json={"brief": "", "style": "3d"}, headers=H).status_code == 400
        assert c.post("/avatar/design", json={"brief": "x" * 601, "style": "3d"}, headers=H).status_code == 400
        r = c.post("/avatar/design", json={"brief": "a robot", "style": "watercolor"}, headers=H)
        assert r.status_code == 400
        assert fake.requests == []   # nothing left the laptop
    finally:
        core.shutdown()


def test_secrets_are_scrubbed_from_the_brief(tmp_path):
    raw = "a robot holding sk_test_1234567890123456 up high"
    core, c, fake = make_core(tmp_path, [image_turn(1)])
    try:
        r = c.post("/avatar/design", json={"brief": raw, "style": "flat"}, headers=H)
        assert r.status_code == 200
        body = r.json()
        assert "sk_test_1234567890123456" not in body["brief"]
        assert "[REDACTED:stripe_key]" in body["brief"]
        sent = fake.requests[0]["input"]
        assert "sk_test_1234567890123456" not in sent and "[REDACTED:stripe_key]" in sent
    finally:
        core.shutdown()


def test_every_image_prompt_refuses_logos_text_and_brand_marks():
    from lantern.avatar import canonical_prompt, expression_prompt
    for prompt in [canonical_prompt("a robot", False), *[expression_prompt(e) for e in EXPRESSIONS]]:
        low = prompt.lower()
        assert "no logos" in low and "no text" in low and "no brand marks" in low


# ---- design + accept --------------------------------------------------------
def test_design_returns_a_preview_and_its_cost(tmp_path):
    core, c, fake = make_core(tmp_path, [image_turn(1)])
    try:
        r = c.post("/avatar/design", json={"brief": "a tiny brass lantern robot", "style": "3d"}, headers=H)
        assert r.status_code == 200
        body = r.json()
        assert body["style"] == "3d" and body["cost_usd"] == pytest.approx(0.01)
        assert body["preview"].startswith("data:image/webp;base64,")
        home = Path(core.cfg.db_path).parent / "avatar" / body["id"]
        assert (home / "idle.webp").read_bytes() == WEBP   # preview only: nothing else drawn
        assert [p.name for p in home.iterdir() if p.suffix == ".webp"] == ["idle.webp"]
        assert core.studio.active_id() is None   # the preview is not on screen yet
        usage = core.memory.usage_by_model()
        assert sum(u["cost_usd"] for u in usage if u["model"] == models.IMAGE) == pytest.approx(0.01)
    finally:
        core.shutdown()


def test_accept_needs_an_approval_showing_the_cost(tmp_path):
    core, c, fake = make_core(tmp_path, [image_turn(i) for i in range(1, 8)])
    try:
        design_id = c.post("/avatar/design", json={"brief": "a robot", "style": "pixel"}, headers=H).json()["id"]
        t, out = accept_in_thread(core, design_id)
        try:
            a = pending(core)
            assert a.action == "finish_avatar"
            assert "7 images, about $0.07" in a.summary
            assert a.payload["id"] == design_id
            core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
            t.join(timeout=30)
            assert out["accepted"] is True and out["verified"] is True
            assert out["model_calls"] == len(EXPRESSIONS) - 1
        finally:
            t.join(timeout=30)
        home = Path(core.cfg.db_path).parent / "avatar" / design_id
        for e in EXPRESSIONS:   # the full sheet, anchored on the preview
            assert (home / f"{e}.webp").read_bytes() == WEBP
        assert core.studio.active_id() == design_id
        rest = fake.requests[1:]
        assert len(rest) == 6 and all(r["previous_response_id"] == "resp_1" for r in rest)
        usage = core.memory.usage_by_model()
        assert sum(u["cost_usd"] for u in usage if u["model"] == models.IMAGE) == pytest.approx(0.07)
    finally:
        core.shutdown()


def test_declining_writes_nothing(tmp_path):
    core, c, fake = make_core(tmp_path, [image_turn(1)])
    try:
        design_id = c.post("/avatar/design", json={"brief": "a robot", "style": "neon"}, headers=H).json()["id"]
        t, out = accept_in_thread(core, design_id)
        try:
            a = pending(core)
            core.bus.decide(a.id, a.nonce, False, by="test")
            t.join(timeout=30)
            assert out == {"accepted": False, "reason": "user declined", "id": design_id}
        finally:
            t.join(timeout=30)
        home = Path(core.cfg.db_path).parent / "avatar" / design_id
        assert [p.name for p in home.iterdir() if p.suffix == ".webp"] == ["idle.webp"]
        assert core.studio.get_meta(design_id)["status"] == "preview"
        assert core.studio.active_id() is None
    finally:
        core.shutdown()


def test_gallery_switch_back_needs_no_model_call(tmp_path):
    core, c, fake = make_core(tmp_path, [image_turn(i) for i in range(1, 8)])
    try:
        first = c.post("/avatar/design", json={"brief": "robot one", "style": "3d"}, headers=H).json()["id"]
        t, _ = accept_in_thread(core, first)
        a = pending(core)
        core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
        t.join(timeout=30)
        n_calls = len(fake.requests)
        r = c.post(f"/avatar/design/{first}/accept", headers=H)   # already finished
        assert r.status_code == 200
        assert r.json() == {"accepted": True, "verified": True, "id": first, "cost_usd": 0.0, "model_calls": 0}
        assert len(fake.requests) == n_calls and core.bus.pending() == []
    finally:
        core.shutdown()


def test_unknown_design_is_404(tmp_path):
    core, c, fake = make_core(tmp_path, [])
    try:
        assert c.post("/avatar/design/nope/accept", headers=H).status_code == 404
        assert c.post("/avatar/design/.../accept", headers=H).status_code == 404
    finally:
        core.shutdown()


# ---- serving the active avatar ----------------------------------------------
def test_manifest_and_files_need_auth_and_refuse_bad_names(tmp_path):
    core, c, fake = make_core(tmp_path, [image_turn(i) for i in range(1, 8)])
    try:
        design_id = c.post("/avatar/design", json={"brief": "a robot", "style": "3d"}, headers=H).json()["id"]
        t, _ = accept_in_thread(core, design_id)
        a = pending(core)
        core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
        t.join(timeout=30)
        assert c.get("/avatar/manifest").status_code == 401
        manifest = c.get("/avatar/manifest", headers=H).json()
        assert manifest["active"] == design_id and len(manifest["expressions"]) == len(EXPRESSIONS)
        assert manifest["designs"][0]["style"] == "3d" and manifest["designs"][0]["active"] is True
        assert c.get(f"/avatar/file/{design_id}/idle").status_code == 401
        r = c.get(f"/avatar/file/{design_id}/idle", headers=H)
        assert r.status_code == 200 and r.content == WEBP
        assert r.headers["content-type"] == "image/webp"
        for bad in (f"/avatar/file/{design_id}/../meta", f"/avatar/file/{design_id}/grumpy",
                    "/avatar/file/nope/idle", f"/avatar/file/{design_id}/idle.webp"):
            assert c.get(bad, headers=H).status_code == 404, bad
    finally:
        core.shutdown()


def test_manifest_before_any_design_is_empty_but_open_for_auth(tmp_path):
    core, c, fake = make_core(tmp_path, [])
    try:
        assert c.get("/avatar/manifest", headers=H).json() == {"active": None, "expressions": {}, "designs": []}
    finally:
        core.shutdown()
