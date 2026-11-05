"""T9: honest time-saved estimates from a fixed table (never a model), stored per task."""
import time

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient
from lantern import timesaved
from lantern.audit import AuditLog
from lantern.config import Config
from lantern.core import Core
from lantern.memory import Memory
from lantern.receipt import as_text, build
from lantern.server import create_app

TOKEN = "test-token"
H = {"X-Lantern-Token": TOKEN}


def mem(tmp_path):
    return Memory(str(tmp_path / "m.db"))


def done_task(m, goal="do the thing", steps=(), status="done", summary="did it"):
    t = m.start_task(goal)
    for kind, content in steps:
        m.log_step(t.id, kind, content)
    m.finish_task(t.id, status, summary)
    return t.id


def tool_result(name, result):
    return ("tool_result", {"name": name, "result": result})


def test_verified_actions_count_and_unverified_do_not(tmp_path):
    m = mem(tmp_path)
    tid = done_task(m, steps=[
        tool_result("fill_form", {"verified": True}),
        tool_result("send_email", {"verified": True}),
        tool_result("open_link", {"verified": True}),
        tool_result("move_file", {"verified": True}),
        tool_result("freecad.export", {"verified": True}),
        tool_result("fill_form", {"verified": False, "reason": "user declined"}),
        tool_result("send_email", {"verified": False}),
        tool_result("remember", {"verified": True}),   # not priced: changes nothing
    ])
    assert m.time_saved(tid) == pytest.approx(6 + 3 + 0.5 + 0.5 + 10)
    minutes, labels = timesaved.estimate_steps(m.steps(tid))
    assert minutes == pytest.approx(20.0)
    assert labels == ["file moved", "form filled", "email sent", "link opened", "CAD part exported"]


def test_failed_and_stopped_tasks_count_zero(tmp_path):
    m = mem(tmp_path)
    tid = done_task(m, status="failed", summary="broke",
                    steps=[tool_result("fill_form", {"verified": True})])
    assert m.time_saved(tid) == 0
    tid2 = m.start_task("tidy")
    m.log_step(tid2.id, "tool_result", {"name": "organize_files", "result": {"verified": True, "moved": 5}})
    m.finish_task(tid2.id, "cancelled", "stopped")
    assert m.time_saved(tid2.id) == 0


def test_move_organize_share_one_per_task_cap(tmp_path):
    m = mem(tmp_path)
    tid = done_task(m, steps=[
        tool_result("organize_files", {"verified": True, "moved": 25}),
        tool_result("move_file", {"verified": True}),
    ])
    assert m.time_saved(tid) == pytest.approx(20 * 0.5)   # capped, not 26 files
    assert m.time_saved(999) == 0   # unknown task: nothing


def test_week_total_across_days_with_fake_clock(tmp_path, monkeypatch):
    m = mem(tmp_path)
    audit = AuditLog(str(tmp_path / "a.db"))
    base = 1_700_000_000  # a Monday
    clock = {"t": float(base)}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    t1 = m.start_task("form day one")
    m.log_step(t1.id, "tool_result", {"name": "fill_form", "result": {"verified": True}})
    m.finish_task(t1.id, "done", "filled")
    clock["t"] = base + 2 * 86400   # Wednesday
    t2 = m.start_task("mail day")
    m.log_step(t2.id, "tool_result", {"name": "send_email", "result": {"verified": True}})
    m.finish_task(t2.id, "done", "sent")
    out = timesaved.summarize(m, audit, days=3, now=base + 2 * 86400 + 3600)
    assert [d["minutes"] for d in out["days"]] == [6.0, 0.0, 3.0]
    assert out["total_min"] == pytest.approx(9.0) and out["total"] == "9 min"
    assert timesaved.summarize(m, audit, days=1)["total_min"] == pytest.approx(3.0)
    with pytest.raises(ValueError):
        timesaved.summarize(m, audit, days=0)
    with pytest.raises(ValueError):
        timesaved.summarize(m, audit, days=99)


def test_verified_localsend_receives_count_from_audit(tmp_path):
    m = mem(tmp_path)
    audit = AuditLog(str(tmp_path / "a.db"))
    audit.record(lane="local", destination="localsend", endpoint="localsend.upload",
                 model="-", payload=b"name,42", task_id=None, preview="accepted photo.jpg")
    out = timesaved.summarize(m, audit, days=7)
    assert out["total_min"] == pytest.approx(1.0)
    assert out["days"][-1]["minutes"] == pytest.approx(1.0)


def test_receipt_carries_the_one_line(tmp_path):
    m = mem(tmp_path)
    audit = AuditLog(str(tmp_path / "a.db"))
    tid = done_task(m, goal="fill the grant form",
                    steps=[tool_result("fill_form", {"verified": True})])
    r = build(m, audit, tid)
    assert r["time_saved_min"] == pytest.approx(6.0) and r["time_saved"] == "form filled"
    assert "• Estimated time saved: 6 min (form filled)" in as_text(r)
    tid2 = done_task(m, goal="look something up")
    assert "• Estimated time saved: 0 min (nothing verified)" in as_text(build(m, audit, tid2))


def test_forgetting_a_task_forgets_its_estimate(tmp_path):
    m = mem(tmp_path)
    tid = done_task(m, steps=[tool_result("fill_form", {"verified": True})])
    assert m.time_saved(tid) > 0
    m.forget_task(tid)
    assert m.time_saved(tid) == 0


def test_route_needs_auth_and_reports_total_and_days(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "a.db"), str(tmp_path / "z.json")
    core = Core(cfg, FakeClient([]), approval_timeout=5, reminder_tick=0)
    try:
        t = core.memory.start_task("fill the grant form")
        core.memory.log_step(t.id, "tool_result", {"name": "fill_form", "result": {"verified": True}})
        core.memory.finish_task(t.id, "done", "filled")
        c = TestClient(create_app(core, TOKEN))
        assert c.get("/stats/time-saved").status_code == 401
        body = c.get("/stats/time-saved", headers=H).json()
        assert body["total_min"] == pytest.approx(6.0) and body["total"] == "6 min"
        assert len(body["days"]) == 7 and body["days"][-1]["minutes"] == pytest.approx(6.0)
        assert c.get("/stats/time-saved?days=3", headers=H).json()["days"].__len__() == 3
        assert c.get("/stats/time-saved?days=0", headers=H).status_code == 400
    finally:
        core.shutdown()


def test_format_minutes():
    assert timesaved.format_minutes(0) == "0 min"
    assert timesaved.format_minutes(0.5) == "30 sec"
    assert timesaved.format_minutes(6) == "6 min"
    assert timesaved.format_minutes(60) == "1 h"
    assert timesaved.format_minutes(135) == "2 h 15 min"
