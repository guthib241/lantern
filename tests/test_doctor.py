"""M21: the setup doctor. Read-only, never shows secrets, says exactly what to fix."""
import json

from fastapi.testclient import TestClient

from fakes import FakeClient
from lantern import doctor
from lantern.config import Config
from lantern.core import Core
from lantern.server import create_app

KEY = "LLM|" + "0123456789abcdef" * 3


def cfg_for(tmp_path, private=True):
    root = tmp_path / "home"
    (root / "Private").mkdir(parents=True, exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.zones_file = str(tmp_path / "zones.json")
    zones = [{"path": str(root), "zone": "writable"}] + ([{"path": str(root / "Private"), "zone": "private"}] if private else [])
    (tmp_path / "zones.json").write_text(json.dumps({"zones": zones}))
    return cfg


def env(vars, which=("ffmpeg", "node", "cloudflared"), modules=True, glimmer_up=True, chromium="/opt/chromium", tracked=""):
    probed = []

    def probe(url):
        probed.append(url)
        return glimmer_up
    e = doctor.Env(vars, which=lambda name: f"/usr/bin/{name}" if name in which else None,
                   has_module=lambda m: modules is True or m not in modules, probe=probe,
                   chromium=lambda setting: chromium, run=lambda argv: tracked)
    return e, probed


READY = {"MODEL_API_KEY": KEY, "LANTERN_FORMS_MODEL": "muse-wonder-1.0", "LANTERN_CUA_PROTOCOL": "native",
         "LANTERN_GLIMMER_URL": "http://127.0.0.1:8080/v1", "LANTERN_CHANNELS": "whatsapp",
         "WHATSAPP_TOKEN": "t" * 40, "WHATSAPP_PHONE_NUMBER_ID": "123", "WHATSAPP_APP_SECRET": "s" * 32,
         "WHATSAPP_VERIFY_TOKEN": "v", "LANTERN_WHATSAPP_OWNER": "919900000000", "LANTERN_EMAIL_MODE": "smtp",
         "LANTERN_SMTP_USER": "asha@example.com", "LANTERN_SMTP_PASSWORD": "abcd efgh ijkl mnop",
         "LANTERN_KEY_STORE": "file"}


def by_id(checks):
    return {c.id: c for c in checks}


def test_a_fully_set_up_laptop(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_avatar_expressions", lambda: 7)
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    (tmp_path / "desktop" / "node_modules" / "electron").mkdir(parents=True)
    e, probed = env(dict(READY))
    checks = doctor.run_checks(cfg_for(tmp_path), e)
    assert {c.id: c.status for c in checks if c.status != "ok"} == {"localsend": "off"}
    assert probed == ["http://127.0.0.1:8080/v1/models"]
    assert doctor.readiness(checks) == {"rehearsal": {"blocked_by": [], "to_do": []}, "demo": {"blocked_by": [], "to_do": []}}
    text = doctor.report(checks)
    assert "Real demo: ready ✓" in text
    for secret in (KEY, "abcd efgh", "t" * 40, "s" * 32):   # secrets are never shown
        assert secret not in text and secret not in json.dumps(doctor.as_json(checks))


def test_a_fresh_laptop_gets_exact_fixes(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    e, probed = env({"LANTERN_CHANNELS": "whatsapp", "LANTERN_WHATSAPP_OWNER": "+91 99000", "LANTERN_EMAIL_MODE": "smtp"},
                    which=(), modules={"playwright", "mcp"}, chromium=None, tracked=".env\n")
    c = by_id(doctor.run_checks(cfg_for(tmp_path, private=False), e))
    assert c["packages"].status == "fail" and "playwright, mcp" in c["packages"].detail
    assert c["chromium"].status == "fail" and "playwright install chromium" in c["chromium"].fix
    assert c["key"].status == "fail" and "scripts/smoke.py" in c["key"].fix
    assert c["env-file"].status == "fail" and "rotate the key" in c["env-file"].fix
    assert c["whatsapp"].status == "fail" and "WHATSAPP_TOKEN" in c["whatsapp"].detail
    assert c["tunnel"].status == "warn" and c["ffmpeg"].status == "fail"
    assert c["email"].status == "fail" and "app password" in c["email"].fix
    assert c["glimmer"].status == "warn" and probed == []
    assert c["zones"].status == "warn" and "--set-zone" in c["zones"].fix
    assert c["desktop"].status == "fail" and "npm install" in c["desktop"].fix
    assert c["avatar"].status == "warn" and "make_avatar.py" in c["avatar"].fix
    ready = doctor.readiness(list(c.values()))
    assert ready["rehearsal"]["blocked_by"] == ["packages", "chromium", "env-file"]
    assert "key" in ready["demo"]["blocked_by"] and "glimmer" in ready["demo"]["to_do"]


def test_bad_owner_number_and_dead_glimmer(tmp_path):
    e, _ = env({**READY, "LANTERN_WHATSAPP_OWNER": "+91 99000 00000"}, glimmer_up=False)
    c = by_id(doctor.run_checks(cfg_for(tmp_path), e))
    assert c["whatsapp"].status == "fail" and "digits only" in c["whatsapp"].detail
    assert c["glimmer"].status == "fail" and "does not answer" in c["glimmer"].detail
    e, probed = env({**READY, "LANTERN_GLIMMER_URL": "https://glimmer.example.modal.run/v1"})
    assert by_id(doctor.run_checks(cfg_for(tmp_path), e))["glimmer"].status == "ok" and probed == []  # remote: not probed


def test_localsend_check_off_listening_and_broken(tmp_path, monkeypatch):
    e, _ = env({})
    assert by_id(doctor.run_checks(cfg_for(tmp_path), e))["localsend"].status == "off"
    monkeypatch.setattr(doctor, "_tcp_open", lambda host, port: True)
    e, _ = env({"LANTERN_LOCALSEND": "1"})
    c = by_id(doctor.run_checks(cfg_for(tmp_path), e))["localsend"]
    assert c.status == "ok" and "listening on port 53317" in c.detail
    (tmp_path / "localsend").mkdir(exist_ok=True)
    (tmp_path / "localsend" / "status.json").write_text(
        json.dumps({"discovery_error": "can't join the discovery group"}))
    cfg = cfg_for(tmp_path)
    cfg.db_path = str(tmp_path / "m.db")
    c = by_id(doctor.run_checks(cfg, env({"LANTERN_LOCALSEND": "1"})[0]))["localsend"]
    assert "discovery degraded" in c.detail
    monkeypatch.setattr(doctor, "_tcp_open", lambda host, port: False)
    c = by_id(doctor.run_checks(cfg, env({"LANTERN_LOCALSEND": "1"})[0]))["localsend"]
    assert c.status == "fail" and "Firewall" in c.fix


def test_server_route_and_cli(tmp_path, monkeypatch, capsys):
    cfg = cfg_for(tmp_path)
    cfg.audit_path = str(tmp_path / "audit.db")
    core = Core(cfg, FakeClient([]), reminder_tick=0)
    monkeypatch.setattr(doctor, "_chromium", lambda setting: "/opt/chromium")
    c = TestClient(create_app(core, "tok"))
    assert c.get("/doctor").status_code == 401
    body = c.get("/doctor", headers={"X-Lantern-Token": "tok"}).json()
    assert {"python", "key", "chromium", "zones"} <= {x["id"] for x in body["checks"]} and "demo" in body["ready"]
    core.shutdown()
    from lantern import cli
    from lantern import config as config_mod
    monkeypatch.setattr(config_mod, "load", lambda: cfg)
    cli.main(["--doctor"])
    out = capsys.readouterr().out
    assert "Lantern doctor" in out and "Rehearsal (--demo-run)" in out
