"""T3c: loop agents (packs that run on a schedule): schedule validation, due-time
math, enabling with approval, scheduled runs, no overlap, pause on failures,
per-run budget, approval timeouts, routes and phone keywords."""
import datetime as dt
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call
from lantern.agentpacks import (
    MAX_LOOP_FAILURES,
    PackError,
    PackStore,
    Schedule,
    schedule_description,
    schedule_next,
    validate,
)
from lantern.channels import ChannelHub, FakeChannel
from lantern.config import Config
from lantern.core import Core
from lantern.server import create_app

TOKEN = "test-token"
H = {"X-Lantern-Token": TOKEN}


def loop_pack(**over) -> dict:
    pack = {"format": "lantern-agent/1", "name": "morning-post", "title": "Morning post",
            "description": "Posts the morning note.", "author": "bea", "version": "1.0.0",
            "inputs": [{"key": "folder", "question": "Which folder?", "example": "Notes"}],
            "goal": "Summarise {folder} for the morning post.",
            "instructions": "List the folder, then answer in one line.",
            "tools": ["list_folders"],
            "schedule": {"every": "day", "at": "09:00", "inputs": {"folder": "Notes"}}}
    pack.update(over)
    return pack


def stamp(day: int, hour: int, minute: int = 0) -> float:
    """A local timestamp: November 2026 (Nov 1 is a Sunday)."""
    return dt.datetime(2026, 11, day, hour, minute).timestamp()


def make_core(tmp_path, script, approval_timeout=30, **env):
    import os
    root = Path(tmp_path) / "docs"
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path = str(tmp_path / "audit.db")
    cfg.zones_file = str(tmp_path / "zones.json")
    cfg.agents_dir = str(tmp_path / "agents")
    for k, v in env.items():
        os.environ[k] = v
    try:
        core = Core(cfg, FakeClient(script), approval_timeout=approval_timeout, reminder_tick=0)
    finally:
        for k in env:
            os.environ.pop(k, None)
    return core, root


def pending(core, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        found = core.bus.pending()
        if found:
            return found[0]
        time.sleep(0.01)
    raise AssertionError("no approval was requested")


def finish_script(summary="posted", reply="posted."):
    return [([call("finish", "c1", status="done", summary=summary)], ""), ([], reply)]


def enable_in_thread(core, name):
    out = {}
    t = threading.Thread(target=lambda: out.update(core.loop_resume(name)))
    t.start()
    return t, out


# ---- schedule validation -------------------------------------------------------
def test_schedule_good_variants():
    day = validate(loop_pack())
    assert day.schedule and day.schedule.every == "day"
    hours = validate(loop_pack(schedule={"every": "hours", "hours": 2, "inputs": {"folder": "N"}}))
    assert hours.schedule and hours.schedule.hours == 2.0
    validate(loop_pack(schedule={"every": "weekday", "at": "17:00", "inputs": {"folder": "N"}}))
    validate(loop_pack(schedule={"every": "week", "at": "17:00", "inputs": {"folder": "N"}}))
    assert schedule_description(Schedule("day", at="09:00")) == "every day at 09:00"
    assert schedule_description(Schedule("hours", hours=2.0)) == "every 2 hours"
    assert schedule_description(Schedule("hours", hours=1.0)) == "every 1 hour"


def test_schedule_refused():
    base = loop_pack()
    with pytest.raises(PackError, match="unknown schedule field"):
        validate(loop_pack(schedule={"every": "day", "at": "09:00", "inputs": {"folder": "N"}, "cron": "*"}))
    with pytest.raises(PackError, match="one of"):
        validate(loop_pack(schedule={"every": "minutely", "at": "09:00", "inputs": {"folder": "N"}}))
    for bad in ["9am", "25:00", "09:60", "", 900, None]:
        with pytest.raises(PackError, match="HH:MM"):
            validate(loop_pack(schedule={"every": "day", "at": bad, "inputs": {"folder": "N"}}))
    for bad in [0.5, 0, -2, "two", True, None]:
        with pytest.raises(PackError, match="hours"):
            validate(loop_pack(schedule={"every": "hours", "hours": bad, "inputs": {"folder": "N"}}))
    with pytest.raises(PackError, match="'hours', not 'at'"):
        validate(loop_pack(schedule={"every": "hours", "hours": 2, "at": "09:00", "inputs": {"folder": "N"}}))
    with pytest.raises(PackError, match="not 'hours'"):
        validate(loop_pack(schedule={"every": "day", "at": "09:00", "hours": 2, "inputs": {"folder": "N"}}))
    with pytest.raises(PackError, match="no such input"):
        validate(loop_pack(schedule={"every": "day", "at": "09:00", "inputs": {"nowhere": "x"}}))
    with pytest.raises(PackError, match="must fill"):
        validate(loop_pack(schedule={"every": "day", "at": "09:00", "inputs": {}}))
    with pytest.raises(PackError, match="at most 500"):
        validate(loop_pack(schedule={"every": "day", "at": "09:00", "inputs": {"folder": "x" * 501}}))
    with pytest.raises(PackError, match="model"):
        validate({**base, "schedule": {"every": "day", "at": "09:00", "inputs": {"folder": "N"}}, "model": "x"})
    with pytest.raises(PackError, match="secret"):
        # Built up so the source holds no key-like token, only the value at run time does.
        key_like = "sk-" + "ant-" + "abcdefghij1234567890"
        validate(loop_pack(schedule={"every": "day", "at": "09:00", "inputs": {"folder": key_like}}))


def test_install_starts_off_and_export_round_trips(tmp_path):
    store = PackStore(tmp_path / "agents")
    store.install(loop_pack())
    assert store.loop_state("morning-post")["enabled"] is False
    raw = store.export("morning-post")
    assert json.loads(raw)["schedule"]["every"] == "day"
    assert [p["loop"]["enabled"] for p in store.list()] == [False]


# ---- due-time math (fake clock) ---------------------------------------------------
def test_due_day():
    sched = Schedule("day", at="09:00")
    assert schedule_next(sched, stamp(2, 8, 0)) == stamp(2, 9, 0)      # Mon 08:00 -> Mon 09:00
    assert schedule_next(sched, stamp(2, 9, 0)) == stamp(3, 9, 0)      # exactly at -> tomorrow
    assert schedule_next(sched, stamp(2, 10, 0)) == stamp(3, 9, 0)


def test_due_weekday_skips_weekend():
    sched = Schedule("weekday", at="09:00")
    assert schedule_next(sched, stamp(2, 8, 0)) == stamp(2, 9, 0)      # Monday -> Monday
    assert schedule_next(sched, stamp(6, 18, 0)) == stamp(9, 9, 0)     # Friday eve -> Monday


def test_due_week_anchors_to_first_run():
    sched = Schedule("week", at="17:00")
    first = schedule_next(sched, stamp(2, 8, 0))                        # Monday 17:00
    assert first == stamp(2, 17, 0)
    assert schedule_next(sched, stamp(2, 18, 0), last=first) == stamp(9, 17, 0)  # next Monday


def test_due_hours():
    sched = Schedule("hours", hours=2.0)
    assert schedule_next(sched, stamp(2, 8, 0)) == stamp(2, 10, 0)
    assert schedule_next(sched, stamp(2, 8, 0), last=stamp(2, 5, 0)) == stamp(2, 9, 0)  # catch up


# ---- enabling ------------------------------------------------------------------
def test_enable_needs_approval_showing_frequency_and_budget(tmp_path):
    core, _ = make_core(tmp_path, [])
    try:
        core.packs.install(loop_pack())
        t, out = enable_in_thread(core, "morning-post")
        a = pending(core)
        assert a.action == "enable_loop"
        assert "every day at 09:00" in a.summary
        assert "$0.10" in a.summary
        assert a.payload["schedule"]["every"] == "day"
        core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
        t.join(timeout=10)
        assert out["enabled"] is True
        state = core.packs.loop_state("morning-post")
        assert state["enabled"] is True and state["next_run"] is not None and state["failures"] == 0
    finally:
        core.shutdown()


def test_enable_decline_keeps_loop_off(tmp_path):
    core, _ = make_core(tmp_path, [])
    try:
        core.packs.install(loop_pack())
        t, out = enable_in_thread(core, "morning-post")
        a = pending(core)
        core.bus.decide(a.id, a.nonce, False, by="test")
        t.join(timeout=10)
        assert out["enabled"] is False
        assert core.packs.loop_state("morning-post")["enabled"] is False
    finally:
        core.shutdown()


def test_enable_refuses_packs_without_schedule(tmp_path):
    core, _ = make_core(tmp_path, [])
    try:
        data = loop_pack()
        del data["schedule"]
        core.packs.install(data)
        with pytest.raises(ValueError, match="no schedule"):
            core.loop_resume("morning-post")
        with pytest.raises(KeyError):
            core.loop_resume("missing")
    finally:
        core.shutdown()


# ---- scheduled runs ---------------------------------------------------------------
def test_disabled_schedule_never_runs(tmp_path):
    core, _ = make_core(tmp_path, finish_script())
    try:
        core.packs.install(loop_pack())
        assert core.check_loops(stamp(3, 10, 0)) == []
        assert core.memory.recent_tasks() == []
    finally:
        core.shutdown()


def test_scheduled_run_is_tainted_allowlisted_task(tmp_path):
    script = [([call("search_files", "c1", query="x")], ""),
              ([call("finish", "c2", status="done", summary="posted")], ""),
              ([], "posted.")]
    core, _ = make_core(tmp_path, script)
    try:
        core.packs.install(loop_pack())
        core.packs.save_loop("morning-post", {**core.packs.loop_state("morning-post"),
                                              "enabled": True, "next_run": stamp(3, 9, 0)})
        fired = core.check_loops(stamp(3, 9, 1))
        assert len(fired) == 1 and core.wait_idle()
        task = core.memory.get_task(fired[0]["task_id"])
        assert task.goal == "[agent:morning-post] Summarise Notes for the morning post."
        assert task.status == "done"
        results = [json.loads(s["content"])["result"]
                   for s in core.memory.steps(task.id) if s["kind"] == "tool_result"]
        assert results[0] == {"error": "this agent pack may not use search_files"}
        assert core.budget.overrides.get(task.id) == pytest.approx(0.10)
    finally:
        core.shutdown()


def test_manual_run_has_no_per_run_budget(tmp_path):
    core, _ = make_core(tmp_path, finish_script())
    try:
        core.packs.install(loop_pack())
        tid = core.submit("", pack="morning-post", inputs={"folder": "Notes"})
        assert core.wait_idle()
        assert tid not in core.budget.overrides
    finally:
        core.shutdown()


def test_no_overlap_while_previous_run_is_active(tmp_path):
    core, _ = make_core(tmp_path, [([call("write_file", "c1", path="n.txt", content="hi")], ""),
                                    ([call("finish", "c2", status="done", summary="w")], ""),
                                    ([], "wrote.")])
    try:
        core.packs.install({**loop_pack(), "tools": ["write_file"]})
        core.packs.save_loop("morning-post", {**core.packs.loop_state("morning-post"),
                                              "enabled": True, "next_run": stamp(3, 9, 0)})
        first = core.check_loops(stamp(3, 9, 1))
        assert len(first) == 1
        pending(core)  # the run is blocked on its card
        assert core.check_loops(stamp(3, 12, 0)) == []   # next due, but still going: no second run
        assert len(core.memory.active_tasks()) == 1
    finally:
        core.shutdown()


def test_pause_after_three_failures_and_user_is_told(tmp_path):
    script = []
    for i in range(6):
        script += [([call("finish", f"c{i}", status="failed", summary=f"bad {i}")], ""), ([], f"bad {i}.")]
    core, _ = make_core(tmp_path, script)
    events = []
    try:
        core.packs.install(loop_pack())
        core.packs.save_loop("morning-post", {**core.packs.loop_state("morning-post"),
                                              "enabled": True, "next_run": stamp(3, 9, 0)})
        core.subscribe(lambda e: events.append(e))
        for day in (3, 4, 5):
            fired = core.check_loops(stamp(day, 9, 1))
            assert len(fired) == 1, f"day {day} should fire"
            assert core.wait_idle()
            core.check_loops(stamp(day, 9, 2))  # settle the finished run
        state = core.packs.loop_state("morning-post")
        assert state["enabled"] is False and state["failures"] == MAX_LOOP_FAILURES
        assert state["last_status"] == "failed"
        paused = [e for e in events if e.get("type") == "loop" and e.get("event") == "paused"]
        assert paused and "3 failed" in paused[0]["text"]
        assert core.check_loops(stamp(6, 9, 1)) == []   # paused: stays quiet
    finally:
        core.shutdown()


def test_stopped_runs_count_toward_pause(tmp_path):
    # A stopped run spends two model turns: the blocked call, then one more turn
    # before the agent sees the kill switch (it is discarded and marked cancelled).
    script = []
    for i in range(3):
        script += [([call("write_file", f"c{i}", path="n.txt", content="hi")], ""),
                   ([call("finish", f"d{i}", status="done", summary="w")], "")]
    core, _ = make_core(tmp_path, script)
    try:
        core.packs.install({**loop_pack(), "tools": ["write_file"]})
        for _ in range(MAX_LOOP_FAILURES):
            core.packs.save_loop("morning-post", {**core.packs.loop_state("morning-post"),
                                                  "enabled": True, "next_run": stamp(3, 9, 0)})
            fired = core.check_loops(stamp(3, 9, 1))
            assert len(fired) == 1
            pending(core)
            core.stop()
            assert core.wait_idle()
            core.check_loops(stamp(3, 9, 2))
        assert core.packs.loop_state("morning-post")["enabled"] is False
    finally:
        core.shutdown()


def test_approval_timeout_means_declined(tmp_path):
    core, _ = make_core(tmp_path, [([call("write_file", "c1", path="n.txt", content="hi")], ""),
                                    ([call("finish", "c2", status="done", summary="wrote it")], ""),
                                    ([], "wrote it.")], approval_timeout=0.05)
    try:
        core.packs.install({**loop_pack(), "tools": ["write_file"]})
        core.packs.save_loop("morning-post", {**core.packs.loop_state("morning-post"),
                                              "enabled": True, "next_run": stamp(3, 9, 0)})
        fired = core.check_loops(stamp(3, 9, 1))
        assert len(fired) == 1
        assert core.wait_idle()
        task = core.memory.get_task(fired[0]["task_id"])
        assert task.status == "done"   # the declined write becomes "user declined", the task finishes
        assert core.bus.pending() == []  # the timed-out card is closed, never left open
    finally:
        core.shutdown()


def test_env_sets_scheduled_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("LANTERN_TASK_BUDGET_USD", "0.25")
    core, _ = make_core(tmp_path, finish_script())
    try:
        core.packs.install(loop_pack())
        assert core.scheduled_budget() == pytest.approx(0.25)
    finally:
        core.shutdown()
    monkeypatch.delenv("LANTERN_TASK_BUDGET_USD")
    core2, _ = make_core(tmp_path / "b", finish_script())
    try:
        core2.packs.install(loop_pack())
        assert core2.scheduled_budget() == pytest.approx(0.10)
    finally:
        core2.shutdown()


# ---- routes -------------------------------------------------------------------------
def make_http(tmp_path, script, approval_timeout=30):
    core, _ = make_core(tmp_path, script, approval_timeout)
    return core, TestClient(create_app(core, TOKEN))


def test_loop_routes_need_token(tmp_path):
    core, http = make_http(tmp_path, [])
    try:
        assert http.get("/loops").status_code == 401
        assert http.post("/agents/x/loop/resume").status_code == 401
        assert http.post("/agents/x/loop/pause").status_code == 401
        assert http.post("/agents/x/loop/run-now").status_code == 401
        assert http.get("/loops", headers=H).json() == {"loops": []}
    finally:
        core.shutdown()


def resume_approved(http, name):
    out = {}
    t = threading.Thread(target=lambda: out.update(
        http.post(f"/agents/{name}/loop/resume", headers=H).json()))
    t.start()
    end = time.time() + 10
    cards = []
    while time.time() < end:
        cards = http.get("/approvals", headers=H).json()
        if cards:
            break
        time.sleep(0.01)
    assert cards and cards[0]["action"] == "enable_loop"
    http.post(f"/approvals/{cards[0]['id']}", headers=H,
              json={"nonce": cards[0]["nonce"], "approve": True,
                    "expected_hash": cards[0]["payload_hash"]}).json()
    t.join(timeout=15)
    assert not t.is_alive()
    return out


def test_loop_routes_round_trip(tmp_path):
    core, http = make_http(tmp_path, finish_script())
    try:
        assert http.post("/agents/nope/loop/resume", headers=H).status_code == 404
        assert http.post("/agents/nope/loop/pause", headers=H).status_code == 404
        assert http.post("/agents/nope/loop/run-now", headers=H).status_code == 404
        core.packs.install(loop_pack())
        loops = http.get("/loops", headers=H).json()["loops"]
        assert len(loops) == 1 and loops[0]["enabled"] is False
        assert "every day at 09:00" in loops[0]["description"]
        r = resume_approved(http, "morning-post")
        assert r["enabled"] is True
        assert http.get("/loops", headers=H).json()["loops"][0]["enabled"] is True
        assert http.get("/agents/morning-post", headers=H).json()["loop"]["enabled"] is True
        run = http.post("/agents/morning-post/loop/run-now", headers=H).json()
        assert core.wait_idle()
        assert core.memory.get_task(run["task_id"]).status == "done"
        assert http.post("/agents/morning-post/loop/pause", headers=H).json() == {
            "name": "morning-post", "enabled": False}
        assert http.get("/loops", headers=H).json()["loops"][0]["enabled"] is False
    finally:
        core.shutdown()


# ---- phone ----------------------------------------------------------------------------
def test_phone_loops_and_pause(tmp_path):
    core, _ = make_core(tmp_path, [])
    try:
        core.packs.install(loop_pack())
        phone = FakeChannel()
        hub = ChannelHub(core, [phone]).start(listen=False)
        try:
            hub.handle(FakeChannel.incoming(text="loops"))
            hub.wait_idle()
            assert "morning-post" in phone.messages()[-1]["text"]
            hub.handle(FakeChannel.incoming(text="pause morning-post"))
            hub.wait_idle()
            assert "paused" in phone.messages()[-1]["text"].lower()
            hub.handle(FakeChannel.incoming(text="pause nope"))
            hub.wait_idle()
            assert "no agent" in phone.messages()[-1]["text"]
        finally:
            hub.stop()
    finally:
        core.shutdown()
