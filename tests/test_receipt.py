"""M31: task receipts, built from Lantern's own records (steps, approvals, the audit log, usage), never by a model."""
from fastapi.testclient import TestClient

from fakes import FakeClient, call, usage
from lantern import cli, models
from lantern.channels import ChannelHub, FakeChannel
from lantern.config import Config
from lantern.core import Core
from lantern.receipt import as_text, build
from lantern.server import create_app

FAKE_AWS = "AKIA" + "Q" * 12 + "7W3E"


def make_core(tmp_path, script, answer=True):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "notes.md").write_text(f"deploy key:\n{FAKE_AWS}\nmeeting at 5", encoding="utf-8")
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "audit.db"), str(tmp_path / "zones.json")
    cfg.media_dir = str(tmp_path / "media")
    core = Core(cfg, FakeClient(script), approval_timeout=5, reminder_tick=0)

    def tap(e):
        if e["type"] == "approval" and e["event"] == "requested":
            core.bus.decide(e["approval"]["id"], e["approval"]["nonce"], answer, by="whatsapp")
    core.subscribe(tap)
    return core, root


SCRIPT = [([call("read_file", "c1", path="notes.md")], "", usage(inp=1000, out=50)),
          ([call("write_file", "c2", path="summary.txt", content="meeting at 5")], "", usage(inp=1200, out=40)),
          ([call("finish", "c3", status="done", summary="summarised")], "", usage(inp=1300, out=10)),
          ([], "Saved. You're welcome.", usage(inp=1400, out=20))]


def test_a_receipt_says_what_changed_what_was_approved_and_what_left(tmp_path):
    core, root = make_core(tmp_path, SCRIPT)
    tid = core.submit("summarise my notes into summary.txt")
    assert core.wait_idle()
    r = build(core.memory, core.audit, tid)
    assert r["task"]["status"] == "done" and r["looked"] == 1
    assert [(a["what"], a["verified"]) for a in r["actions"]] == [("Saved a file", True)]
    assert r["checked"] == 1 and r["unchecked"] == 0
    [a] = r["approvals"]
    assert (a["action"], a["status"], a["by"], a["used"]) == ("write_file", "approved", "whatsapp", True)
    assert len(a["payload_hash"]) == 64
    [sent] = r["sent"]
    assert sent["to"] == "api.meta.ai" and sent["kinds"] == ["model"] and sent["calls"] == 4 and sent["bytes"] > 0
    assert r["redactions"] == {"aws_access_key": 1} and r["left_bytes"] == sent["bytes"]
    assert [m["model"] for m in r["models"]] == [models.SPARK] and r["models"][0]["calls"] == 4
    assert r["cost_usd"] > 0
    text = as_text(r)
    assert text.splitlines()[0] == f"🧾 Task #{tid} · done: summarise my notes into summary.txt"
    assert "• Saved a file: ✓ checked" in text and "• Looked things up 1 time" in text
    assert f"• You approved on WhatsApp, used once: Save the file (bound to {a['payload_hash'][:12]})" in text
    assert "to api.meta.ai (model)" in text and "• Redacted before sending: aws access key ×1" in text
    assert FAKE_AWS not in text
    core.shutdown()


def test_a_declined_change_shows_as_not_done(tmp_path):
    core, root = make_core(tmp_path, SCRIPT, answer=False)
    tid = core.submit("summarise my notes into summary.txt")
    assert core.wait_idle()
    r = build(core.memory, core.audit, tid)
    assert [(a["what"], a["verified"], a["note"]) for a in r["actions"]] == [("Saved a file", False, "user declined")]
    assert r["approvals"][0]["status"] == "denied" and r["approvals"][0]["used"] is False
    assert "• Saved a file: ✗ user declined" in as_text(r) and "• You denied on WhatsApp: Save the file" in as_text(r)
    core.shutdown()


def test_rehearsal_receipts_say_nothing_left(tmp_path):
    from lantern import demo
    cfg = demo.prepare(tmp_path / "rehearsal")
    core, hub, phone = demo.build(cfg, locate=lambda: {})
    tid = core.submit("Which projects use torch?")
    assert core.wait_idle(30)
    r = build(core.memory, core.audit, tid)
    assert r["left_bytes"] == 0 and r["cost_usd"] == 0
    assert all(s["simulated"] for s in r["sent"] if s["lane"] != "local")
    assert "• Would have sent: " in as_text(r) and "to Meta's API (model). Simulated: nothing actually left" in as_text(r)
    assert "• Sent:" not in as_text(r)
    hub.stop()
    core.shutdown()


def test_server_phone_and_cli(tmp_path, capsys, monkeypatch):
    core, root = make_core(tmp_path, SCRIPT)
    phone = FakeChannel()
    hub = ChannelHub(core, [phone]).start(listen=False)
    c = TestClient(create_app(core, "tok", hub=hub))
    h = {"X-Lantern-Token": "tok"}
    hub.receive(phone.incoming("receipt"))
    hub.wait_idle()
    assert phone.messages()[-1]["text"] == "No finished tasks yet, so no receipt."
    tid = core.submit("summarise my notes into summary.txt")
    assert core.wait_idle()
    body = c.get(f"/tasks/{tid}/receipt", headers=h).json()
    assert body["task"]["id"] == tid and body["text"].startswith("🧾")
    assert c.get("/tasks/999/receipt", headers=h).status_code == 404
    assert c.get(f"/tasks/{tid}/receipt").status_code == 401
    for asked in ("receipt", f"receipt #{tid}", f"Receipt for task {tid}"):
        hub.receive(phone.incoming(asked))
        hub.wait_idle()
        assert phone.messages()[-1]["text"].startswith(f"🧾 Task #{tid}"), asked
    hub.receive(phone.incoming("receipt 99"))
    hub.wait_idle()
    assert phone.messages()[-1]["text"] == "There's no task #99."
    hub.stop()
    core.shutdown()
    monkeypatch.setenv("LANTERN_HOME", str(tmp_path))
    monkeypatch.setattr(cli.config_mod, "load", lambda: core.cfg)
    cli.main(["--receipt", str(tid)])
    assert capsys.readouterr().out.startswith(f"🧾 Task #{tid} · done")


def test_private_lane_rows_belong_to_their_task_and_go_when_it_is_forgotten(tmp_path, monkeypatch):
    monkeypatch.setenv("LANTERN_GLIMMER_MODE", "fake")
    script = [([call("ask_private", "c1", path="Private/letter.txt", question="What is the base salary?")], ""),
              ([], "INR 24,00,000.")]
    core, root = make_core(tmp_path, script)
    (root / "Private").mkdir()
    (root / "Private" / "letter.txt").write_text("Base salary: INR 24,00,000 per year", encoding="utf-8")
    core.policy.set_zone(root / "Private", "private")
    tid = core.submit("what does my offer letter say about salary?")
    assert core.wait_idle()
    r = build(core.memory, core.audit, tid)
    assert r["stayed_local"] == 1 and "• Stayed on this computer: 1 private-lane question" in as_text(r)
    assert [e["lane"] for e in core.audit.for_task(tid)].count("local") == 1
    core.forget_task(tid)
    assert core.audit.for_task(tid) == []
    assert not [e for e in core.audit.entries() if e["lane"] == "local"]   # the question's preview is gone too
    core.shutdown()
