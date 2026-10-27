"""M30: spending limits, enforced in code before every model call."""
import time

import pytest
from fastapi.testclient import TestClient

from fakes import FakeClient, call, usage
from lantern.budget import BudgetError, BudgetGuard, Limits
from lantern.config import Config
from lantern.core import Core
from lantern.doctor import Env, run_checks
from lantern.memory import Memory
from lantern.reliability import RetryingClient, explain
from lantern.server import create_app


def test_limits_come_from_the_environment():
    assert Limits.from_env({}) == Limits(total=150.0, daily=None, task=3.0)
    lim = Limits.from_env({"LANTERN_BUDGET_USD": "$40", "LANTERN_DAILY_BUDGET_USD": "5.5", "LANTERN_TASK_BUDGET_USD": "off"})
    assert lim == Limits(total=40.0, daily=5.5, task=None)
    assert Limits.from_env({"LANTERN_BUDGET_USD": "0"}).total is None
    typo = {"LANTERN_BUDGET_USD": "fifty"}
    assert Limits.from_env(typo).total == 150.0                     # a typo never switches the limit off
    assert Limits.from_env(typo).problems(typo) == ["LANTERN_BUDGET_USD='fifty' isn't a number"]


def test_the_ledger_counts_what_was_spent_even_after_forgetting(tmp_path):
    mem = Memory(tmp_path / "m.db")
    t = mem.start_task("expensive")
    mem.log_usage(t.id, "muse-spark-1.3", 1000, 0, 100, 0.40)
    mem.log_usage(None, "sam-3.1", cost_usd=0.10)
    mem.log_usage(t.id, "muse-glimmer", 500, 0, 50, 0.0)
    assert mem.spent() == pytest.approx(0.50) and mem.spent(time.strftime("%Y-%m-%d")) == pytest.approx(0.50)
    assert mem.spent("2001-01-01") == 0
    mem.forget_task(t.id)
    mem.forget_everything()
    assert mem.total_cost() == 0 and mem.spent() == pytest.approx(0.50)   # the money is gone either way


def test_an_older_memory_starts_its_ledger_from_usage(tmp_path):
    mem = Memory(tmp_path / "m.db")
    mem.log_usage(None, "muse-spark-1.3", cost_usd=1.25)
    mem.db.execute("DELETE FROM spend")                                  # as if written before the ledger existed
    mem.db.commit()
    mem.close()
    assert Memory(tmp_path / "m.db").spent() == pytest.approx(1.25)


def guarded(mem, limits, task=None, events=None):
    fake = FakeClient([([], "ok")] * 5)
    guard = BudgetGuard(mem, limits, lambda: task, (events if events is not None else []).append)
    return RetryingClient(fake, before_call=guard.check, sleep=lambda s: None), fake


def test_total_limit_stops_the_call_before_it_leaves(tmp_path):
    mem = Memory(tmp_path / "m.db")
    events = []
    client, fake = guarded(mem, Limits(total=1.0, daily=None, task=None), events=events)
    mem.log_usage(None, "muse-spark-1.3", cost_usd=0.85)
    client.responses.create(model="m", input="hi")                       # 85%: allowed, with one warning
    client.responses.create(model="m", input="hi")
    assert [e["type"] for e in events] == ["budget"] and events[0]["limit_usd"] == 1.0
    mem.log_usage(None, "muse-spark-1.3", cost_usd=0.20)
    with pytest.raises(BudgetError) as e:
        client.responses.create(model="m", input="hi")
    assert len(fake.requests) == 2                                       # refused before the wire, never retried
    assert "spent $1.05 of its $1.00 limit" in str(e.value) and "LANTERN_BUDGET_USD" in explain(e.value)
    assert client.breaker.failures == 0                                  # a limit isn't an outage


def test_daily_and_per_task_limits(tmp_path):
    mem = Memory(tmp_path / "m.db")
    t = mem.start_task("loop")
    client, fake = guarded(mem, Limits(total=None, daily=None, task=0.5), task=t.id)
    mem.log_usage(t.id, "muse-wonder-1.0", cost_usd=0.6)
    with pytest.raises(BudgetError, match=r"task reached its \$0.50 limit"):
        client.responses.create(model="m", input="hi")
    other, _ = guarded(mem, Limits(total=None, daily=None, task=0.5), task=None)
    other.responses.create(model="m", input="hi")                       # outside a task only total/daily apply
    daily, _ = guarded(mem, Limits(total=None, daily=0.5, task=None))
    with pytest.raises(BudgetError, match="daily limit"):
        daily.responses.create(model="m", input="hi")
    nolimits, _ = guarded(mem, Limits(total=None, daily=None, task=None), task=t.id)
    nolimits.responses.create(model="m", input="hi")


def make_core(tmp_path, script, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    root = tmp_path / "docs"
    root.mkdir()
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "audit.db"), str(tmp_path / "zones.json")
    cfg.media_dir = str(tmp_path / "media")
    fake = FakeClient(script)
    return Core(cfg, fake, reminder_tick=0), fake


def test_a_runaway_task_is_stopped_with_a_plain_reason(tmp_path, monkeypatch):
    big = usage(inp=3_000_000)                                           # $3.75 of Spark input in one call
    script = [([call("list_folders", "c1")], "", big), ([call("list_folders", "c2")], "", big), ([], "never")]
    core, fake = make_core(tmp_path, script, monkeypatch, LANTERN_TASK_BUDGET_USD="3")
    tid = core.submit("look around forever")
    assert core.wait_idle()
    t = core.memory.get_task(tid)
    assert t.status == "failed" and t.summary.startswith("This task reached its $3.00 limit (LANTERN_TASK_BUDGET_USD)")
    assert len(fake.requests) == 1
    c = TestClient(create_app(core, "tok"))
    b = c.get("/usage", headers={"X-Lantern-Token": "tok"}).json()["budget"]
    assert b["task_usd"] == 3.0 and b["total_usd"] == 150.0 and b["spent_usd"] == pytest.approx(3.75)
    assert b["left_usd"] == pytest.approx(146.25)
    core.shutdown()


def test_doctor_reports_the_limits():
    def row(env):
        return [c for c in run_checks(Config(), Env(env)) if c.id == "budget"][0]
    assert row({}).status == "ok" and row({}).detail == "$150 total, no daily limit, $3 per task"
    off = row({"LANTERN_BUDGET_USD": "off"})
    assert off.status == "warn" and "LANTERN_BUDGET_USD=150" in off.fix
    assert "isn't a number" in row({"LANTERN_TASK_BUDGET_USD": "three"}).detail


def test_tiny_limits_are_shown_exactly():
    """Live run, Oct 27: a $0.002 per-task limit was reported as "$0.00"."""
    from lantern.budget import usd
    assert [usd(x) for x in (150, 3, 0.002, 0.25, 0.0125, 0)] == ["$150.00", "$3.00", "$0.002", "$0.25", "$0.0125", "$0.00"]
