"""M20: tasks cut short by a crash or restart are marked, reported, and resumed without redoing work."""
import json
import socket

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call
from lantern import cli
from lantern import config as config_mod
from lantern.channels import ChannelHub, FakeChannel
from lantern.config import Config
from lantern.core import Core, recover_interrupted, resume_goal
from lantern.memory import Memory
from lantern.server import create_app


def config(tmp_path):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "audit.db"), str(tmp_path / "zones.json")
    cfg.media_dir = str(tmp_path / "media")
    return cfg


def crashed_run(tmp_path):
    """A previous Lantern that died mid-task: the form went in, the email never happened."""
    mem = Memory(tmp_path / "m.db")
    t = mem.start_task("[whatsapp] fill the reimbursement form and email the organiser")
    mem.log_step(t.id, "tool_result", {"name": "fill_form", "result": {"verified": True, "done": True}})
    mem.log_step(t.id, "tool_result", {"name": "search_files", "result": {"error": "no such folder"}})
    mem.close()
    return t.id


def test_startup_marks_cut_short_tasks_and_resume_says_what_was_done(tmp_path):
    tid = crashed_run(tmp_path)
    core = Core(config(tmp_path), FakeClient([([], "Emailed.")]), reminder_tick=0)
    assert [t.id for t in core.interrupted] == [tid]
    t = core.memory.get_task(tid)
    assert t.status == "interrupted" and "resume" in t.summary
    goal = resume_goal(core.memory, t)
    assert goal.startswith("[whatsapp] fill the reimbursement form and email the organiser\n\n[Resuming task #")
    assert "fill_form (verified)" in goal and "search_files (error: no such folder)" in goal
    events = []
    core.subscribe(events.append)
    [new] = core.resume()
    assert core.wait_idle()
    assert core.memory.get_task(tid).status == "resumed"
    assert core.memory.get_task(new).status == "done"
    assert core.memory.get_task(new).goal.count("[Resuming task #") == 1
    assert {"type": "resumed", "from": tid, "task_id": new}.items() <= events[[e["type"] for e in events].index("resumed")].items()
    assert core.resume() == [] and core.interrupted == []
    core.shutdown()


def test_a_resumed_goal_never_stacks_notes(tmp_path):
    mem = Memory(tmp_path / "m.db")
    t = mem.start_task("tidy downloads\n\n[Resuming task #1, which was cut short ...]")
    assert resume_goal(mem, t).count("[Resuming task #") == 1
    assert recover_interrupted(mem)[0].status == "interrupted"


def test_phone_hears_about_it_and_can_say_resume(tmp_path):
    tid = crashed_run(tmp_path)
    core = Core(config(tmp_path), FakeClient([([], "Done, finally.")]), reminder_tick=0)
    phone = FakeChannel()
    hub = ChannelHub(core, [phone]).start(listen=False)
    hub.wait_idle()
    assert phone.messages()[0]["text"].startswith(f"🏮 I was stopped in the middle of 1 task: #{tid}")
    hub.receive(phone.incoming("resume"))
    hub.wait_idle()
    assert core.wait_idle()
    hub.wait_idle()
    texts = [m["text"] for m in phone.messages()]
    assert any(x.startswith("🏮 Resuming 1 task") for x in texts) and "Done, finally." in texts
    hub.receive(phone.incoming("resume"))
    hub.wait_idle()
    assert phone.messages()[-1]["text"] == "Nothing to resume."
    hub.stop()
    core.shutdown()


def test_server_hello_lists_them_and_resume_route(tmp_path):
    tid = crashed_run(tmp_path)
    core = Core(config(tmp_path), FakeClient([([], "ok")]), reminder_tick=0)
    c = TestClient(create_app(core, "tok"))
    with c.websocket_connect("/events?token=tok") as ws:
        hello = ws.receive_json()
    assert hello["interrupted"] == [{"id": tid, "goal": "[whatsapp] fill the reimbursement form and email the organiser"}]
    r = c.post(f"/tasks/{tid}/retry", headers={"X-Lantern-Token": "tok"}).json()   # retry = resume here
    assert core.wait_idle() and core.memory.get_task(tid).status == "resumed"
    assert "[Resuming task" in core.memory.get_task(r["task_id"]).goal
    assert c.post("/tasks/resume", headers={"X-Lantern-Token": "tok"}).json() == {"task_ids": []}
    core.shutdown()


def test_second_server_is_refused_with_a_clear_message():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert cli.server_running(port)
        with pytest.raises(SystemExit, match="already running"):
            cli.refuse_second_server(port)
    assert not cli.server_running(port)


def test_cli_resume_without_a_server(tmp_path, monkeypatch, capsys):
    tid = crashed_run(tmp_path)
    cfg = config(tmp_path)
    cfg.encrypt = False
    monkeypatch.setattr(config_mod, "load", lambda: cfg)
    fake = FakeClient([([call("finish", "c1", status="done", summary="emailed")], ""), ([], "Emailed the organiser.")])
    monkeypatch.setattr(cli, "terminal_core", lambda cfg_, recover=False: Core(cfg_, fake, workers=1, recover=recover,
                                                                                 reminder_tick=0))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]   # nothing listens here
    cli.main(["--resume", "--port", str(free)])
    out = capsys.readouterr().out
    assert "Emailed the organiser." in out and core_status(cfg, tid) == "resumed"
    assert "fill_form (verified)" in json.dumps(fake.requests[0]["input"])   # told what the cut-short run did
    cli.main(["--resume", "--port", str(free)])
    assert "Nothing to resume." in capsys.readouterr().out


def core_status(cfg, task_id):
    m = Memory(cfg.db_path)
    try:
        return m.get_task(task_id).status
    finally:
        m.close()
