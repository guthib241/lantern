"""M11: Lantern's face from Muse Image (anchored expressions) or imported Muse art, offline."""
import base64
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from fakes import FakeClient
from lantern import models
from lantern.avatar import EXPRESSIONS, AvatarError, generate, plan
from lantern.config import Config

ROOT = Path(__file__).resolve().parent.parent
WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00/\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def image_turn(n=1):
    return ([NS(type="reasoning", id="rs"), NS(type="message", id="msg", content=[]),
             NS(type="image_generation_call", id=f"ig{n}", status="completed", result=base64.b64encode(WEBP).decode())], "")


def test_plan_anchors_every_expression_on_the_canonical():
    turns = plan()
    assert [t.expression for t in turns] == list(EXPRESSIONS)
    assert not turns[0].chained and all(t.chained for t in turns[1:])
    assert "same character" in turns[3].prompt.lower() and "no text" in turns[0].prompt.lower()
    with pytest.raises(AvatarError):
        plan(only=["grumpy"])


def test_generate_sheet_with_muse_image(tmp_path):
    client = FakeClient([image_turn(i) for i in range(7)])
    costs = []
    sheet = generate(client, tmp_path, on_cost=costs.append)
    first, rest = client.requests[0], client.requests[1:]
    assert first["model"] == models.IMAGE == "muse-image-1.0"
    assert "previous_response_id" not in first and isinstance(first["input"], str)
    assert first["tools"] == [{"type": "image_generation", "size": "1024x1024"}]
    assert all(r["previous_response_id"] == "resp_1" for r in rest)   # anchored on the canonical turn
    for e in EXPRESSIONS:
        assert (tmp_path / f"{e}.webp").read_bytes() == WEBP
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["made_with"] == "Muse Image" and manifest["canonical_response"] == "resp_1"
    assert manifest["expressions"]["smug"]["file"] == "smug.webp"
    js = (tmp_path / "manifest.js").read_text()
    assert json.loads(js.split("window.LanternAvatarManifest = ", 1)[1].rstrip().rstrip(";")) == manifest
    assert sheet.cost_usd == pytest.approx(0.07) and len(costs) == 7


def test_start_from_your_own_muse_art(tmp_path):
    client = FakeClient([image_turn(1), image_turn(2)])
    generate(client, tmp_path, reference=(b"\x89PNG fake", ".png"), only=["idle", "smug"])
    msg = client.requests[0]["input"][0]
    assert msg["role"] == "user" and [p["type"] for p in msg["content"]] == ["input_text", "input_image"]
    assert msg["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert "MetaMuse" in msg["content"][0]["text"]
    assert client.requests[1]["previous_response_id"] == "resp_1"
    with pytest.raises(AvatarError, match="PNG, JPEG or WebP"):
        generate(FakeClient([]), tmp_path / "x", reference=(b"GIF89a", ".gif"))


def test_redo_some_expressions_on_the_saved_canonical(tmp_path):
    generate(FakeClient([image_turn(i) for i in range(7)]), tmp_path)
    again = FakeClient([image_turn(9)])
    generate(again, tmp_path, only=["annoyed"])
    assert len(again.requests) == 1 and again.requests[0]["previous_response_id"] == "resp_1"
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert set(manifest["expressions"]) == set(EXPRESSIONS)
    fresh = FakeClient([image_turn(1), image_turn(2)])   # no saved anchor yet: render it first
    generate(fresh, tmp_path / "fresh", only=["annoyed"])
    assert "previous_response_id" not in fresh.requests[0] and fresh.requests[1]["previous_response_id"] == "resp_1"


def test_missing_image_is_an_error(tmp_path):
    with pytest.raises(AvatarError, match="no image"):
        generate(FakeClient([([NS(type="message", content=[])], "I can't draw that")]), tmp_path)


def test_make_avatar_script_dry_run_and_logging(tmp_path, monkeypatch, capsys):
    mod = load_script("make_avatar")
    assert mod.main(["--dry-run", "--out", str(tmp_path)]) == 0
    assert "7 images" in capsys.readouterr().out and not list(tmp_path.iterdir())
    cfg = Config(roots=[str(tmp_path)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path = str(tmp_path / "a.db")
    monkeypatch.setattr(mod.config_mod, "load", lambda: cfg)
    assert mod.main(["--out", str(tmp_path / "avatar")], client=FakeClient([image_turn(i) for i in range(7)])) == 0
    from lantern.audit import AuditLog
    from lantern.memory import Memory
    assert Memory(cfg.db_path).usage_by_model()[0]["cost_usd"] == pytest.approx(0.07)
    assert AuditLog(cfg.audit_path).summary()["cloud"]["by_model"][models.IMAGE]["calls"] == 7


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_import_metamuse_and_vibes_exports(tmp_path, capsys):
    src = tmp_path / "from-app"
    src.mkdir()
    (src / "idle.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (src / "notes.txt").write_text("ignore me")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=24:duration=1",
                    "-pix_fmt", "yuv420p", str(src / "celebrating.mp4")], check=True)
    out = tmp_path / "avatar"
    mod = load_script("import_avatar")
    assert mod.main([str(src), "--out", str(out)]) == 0
    assert (out / "idle.png").read_bytes().startswith(b"\x89PNG")
    webm = (out / "celebrating.webm").read_bytes()
    assert webm[:4] == b"\x1a\x45\xdf\xa3"   # Matroska/WebM
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["expressions"]["celebrating"]["file"] == "celebrating.webm"
    assert "MetaMuse" in manifest["expressions"]["idle"]["source"]
    assert "Missing (they'll show idle)" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        mod.main([str(src / "notes.txt"), "--out", str(out)])


def test_desktop_ships_an_empty_manifest_until_muse_art_exists():
    js = (ROOT / "desktop" / "assets" / "avatar" / "manifest.js").read_text()
    assert "window.LanternAvatarManifest" in js
