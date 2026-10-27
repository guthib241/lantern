"""M18: reminders. Parsed and checked in code, stored encrypted, delivered to the phone and the desktop."""
import datetime as dt
import time

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call
from lantern.agent import Agent
from lantern.channels import ChannelHub, FakeChannel
from lantern.config import Config
from lantern.core import Core
from lantern.crypto import load_cipher
from lantern.memory import Memory
from lantern.reminders import ReminderError, describe, next_due, parse_when
from lantern.server import create_app
from lantern.tools import Toolbox, ToolError

NOW = dt.datetime(2026, 10, 27, 14, 30).astimezone()


def test_parse_when_understands_the_usual_ways_of_saying_it():
    assert parse_when("in 20 minutes", NOW) == NOW + dt.timedelta(minutes=20)
    assert parse_when("in 2 hours", NOW) == NOW + dt.timedelta(hours=2)
    assert parse_when("in 1 day", NOW) == NOW + dt.timedelta(days=1)
    assert parse_when("18:00", NOW).hour == 18 and parse_when("18:00", NOW).date() == NOW.date()
    assert parse_when("9:15", NOW).date() == NOW.date() + dt.timedelta(days=1)  # passed today -> tomorrow
    assert parse_when("6pm", NOW).hour == 18 and parse_when("12am", NOW).hour == 0
    assert parse_when("at 7:30 pm", NOW).strftime("%H:%M") == "19:30"
    iso = parse_when("2026-10-28T09:00", NOW)
    assert (iso.year, iso.month, iso.day, iso.hour) == (2026, 10, 28, 9) and iso.tzinfo is not None
    aware = parse_when("2026-10-28T09:00+00:00", NOW)
    assert aware.timestamp() == dt.datetime(2026, 10, 28, 9, tzinfo=dt.timezone.utc).timestamp()


@pytest.mark.parametrize("bad, why", [("", "say when"), ("in 3 fortnights", "unknown unit"), ("25:00", "clock"),
                                      ("13pm", "clock"), ("2026-10-01T09:00", "past"), ("in 400 days", "year"),
                                      ("next blue moon", "can't read")])
def test_parse_when_refuses_with_a_hint(bad, why):
    with pytest.raises(ReminderError, match=why):
        parse_when(bad, NOW)


def test_repeats_keep_the_wall_clock_time():
    due = NOW.replace(hour=9, minute=0).timestamp()
    nxt = next_due(due, "daily", due + 1)
    assert dt.datetime.fromtimestamp(nxt).strftime("%H:%M") == "09:00" and nxt > due
    assert next_due(due, "weekly", due + 1) - due == pytest.approx(7 * 86400, abs=3600)
    assert next_due(due, "", due + 1) is None
    assert describe(time.time() + 60).startswith("today") or describe(time.time() + 60).startswith("tomorrow")


def toolbox(tmp_path, answer=True, cipher=None):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    mem = Memory(tmp_path / "m.db", cipher)
    asked = []

    def gate(summary):
        asked.append(summary)
        return answer
    return Toolbox([root], mem, gate), mem, asked


def test_set_list_cancel_and_stored_encrypted(tmp_path):
    tb, mem, asked = toolbox(tmp_path, cipher=load_cipher(tmp_path, "file"))
    r = tb.set_reminder("submit the SECRET-PLAN video", "in 2 hours")
    assert r["done"] and r["verified"] and r["id"] == 1 and asked == []  # trusted context: no card needed
    assert b"SECRET-PLAN" not in (tmp_path / "m.db").read_bytes()
    assert tb.list_reminders()["reminders"][0]["text"] == "submit the SECRET-PLAN video"
    assert "Upcoming reminders" in mem.context_block() and "SECRET-PLAN" in mem.context_block()
    assert tb.cancel_reminder(1) == {"done": True, "verified": True, "id": 1}
    assert tb.list_reminders()["count"] == 0
    assert tb.cancel_reminder(1)["reason"] == "reminder #1 is already cancelled"
    with pytest.raises(ToolError):
        tb.cancel_reminder(99)
    with pytest.raises(ToolError, match="past"):
        tb.set_reminder("x", "2020-01-01T09:00")
    with pytest.raises(ToolError, match="repeat"):
        tb.set_reminder("x", "in 1 hour", repeat="hourly")


def test_untrusted_context_needs_approval_and_a_no_stores_nothing(tmp_path):
    tb, mem, asked = toolbox(tmp_path, answer=False)
    tb.tainted = True
    r = tb.set_reminder("click http://evil.test to claim", "in 5 minutes")
    assert r == {"done": False, "reason": "user declined"} and len(asked) == 1
    assert "click http://evil.test" in asked[0] and "[after reading untrusted content]" in asked[0]
    assert mem.reminders() == []


def make_core(tmp_path, script=()):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "audit.db"), str(tmp_path / "zones.json")
    cfg.media_dir = str(tmp_path / "media")
    return Core(cfg, FakeClient(list(script)), approval_timeout=5, reminder_tick=0)


def test_due_reminders_reach_phone_and_desktop_once_and_repeats_move_on(tmp_path):
    core = make_core(tmp_path)
    phone = FakeChannel()
    hub = ChannelHub(core, [phone]).start(listen=False)
    events = []
    core.subscribe(events.append)
    now = time.time()
    once = core.memory.add_reminder("call Priya", now - 1)
    late = core.memory.add_reminder("water the plant", now - 3600)
    daily = core.memory.add_reminder("standup", now - 5, repeat="daily")
    later = core.memory.add_reminder("not yet", now + 3600)
    fired = core.check_reminders(now)
    assert [e["id"] for e in fired] == [late.id, daily.id, once.id]
    assert {e["id"]: e["late"] for e in fired} == {late.id: True, daily.id: False, once.id: False}
    assert core.check_reminders(now + 1) == []          # delivered once
    assert core.memory.get_reminder(once.id).status == "delivered"
    assert core.memory.get_reminder(daily.id).status == "pending"
    assert core.memory.get_reminder(daily.id).due_at > now
    assert core.memory.get_reminder(later.id).status == "pending"
    hub.wait_idle()
    texts = [m["text"] for m in phone.messages()]
    assert "⏰ Reminder: call Priya" in texts
    assert "⏰ Reminder: water the plant (it was due while Lantern was off)" in texts
    assert [e for e in events if e["type"] == "reminder"][0]["text"] == "water the plant"
    hub._text(phone, "status")
    hub.wait_idle()
    assert "Reminders:" in phone.messages()[-1]["text"]
    hub.stop()
    core.shutdown()


def test_scheduler_thread_delivers_on_its_own(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "audit.db"), str(tmp_path / "zones.json")
    core = Core(cfg, FakeClient([]), reminder_tick=0.05)
    events = []
    core.subscribe(events.append)
    core.memory.add_reminder("tea", time.time() + 0.1)
    for _ in range(100):
        if any(e["type"] == "reminder" for e in events):
            break
        time.sleep(0.02)
    core.shutdown()
    assert [e["text"] for e in events if e["type"] == "reminder"] == ["tea"]


def test_server_lists_and_cancels(tmp_path):
    core = make_core(tmp_path)
    c = TestClient(create_app(core, "tok"))
    h = {"X-Lantern-Token": "tok"}
    r = core.memory.add_reminder("send invoice", time.time() + 600)
    assert [x["text"] for x in c.get("/reminders", headers=h).json()] == ["send invoice"]
    assert c.post(f"/reminders/{r.id}/cancel", headers=h).json() == {"cancelled": r.id}
    assert c.post(f"/reminders/{r.id}/cancel", headers=h).status_code == 409
    assert c.post("/reminders/999/cancel", headers=h).status_code == 404
    assert c.get("/reminders").status_code == 401
    core.shutdown()


def test_agent_sets_a_reminder_from_plain_words(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    mem = Memory(tmp_path / "m.db")
    fake = FakeClient([
        ([call("set_reminder", "c1", text="call Priya", when="in 20 minutes")], ""),
        ([call("finish", "c2", status="done", summary="reminder set")], ""),
        ([], "Done. I'll nag you in 20 minutes."),
    ])
    r = Agent(fake, mem, Toolbox([root], mem, lambda d: True)).run("remind me in 20 minutes to call Priya")
    assert r.status == "done" and r.tool_calls[0]["result"]["verified"]
    assert mem.reminders()[0].text == "call Priya"
    assert "set_reminder" in [t["name"] for t in fake.requests[0]["tools"]]


def test_forgetting_a_task_forgets_its_reminders(tmp_path):
    mem = Memory(tmp_path / "m.db")
    t = mem.start_task("remind me")
    mem.add_reminder("secret thing", time.time() + 60, task_id=t.id)
    mem.add_reminder("keep me", time.time() + 60)
    assert mem.forget_task(t.id)["reminders"] == 1
    assert [r.text for r in mem.reminders()] == ["keep me"]
    assert mem.forget_everything()["reminders"] == 1


def test_bad_tool_input_comes_back_as_an_error_not_a_crash(tmp_path):
    tb, mem, _ = toolbox(tmp_path)
    with pytest.raises(ToolError, match="year"):
        tb.call("set_reminder", {"text": "x", "when": "in 99999999999 days"})
    with pytest.raises(ToolError, match="empty"):
        tb.call("remember", {"key": " ", "value": "x"})
    fake = FakeClient([([call("remember", "c1", key=" ", value="x")], ""), ([], "Fixed it.")])
    r = Agent(fake, mem, tb).run("remember something")
    assert r.status == "done" and "error" in r.tool_calls[0]["result"]   # the model saw the error and carried on
