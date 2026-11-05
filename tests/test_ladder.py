"""S3: the model ladder. Forced failures escalate, repeated success de-escalates, private content never goes
above the local rung. All model calls are scripted (FakeClient per task); nothing reaches the real API."""
import json

import pytest
from fastapi.testclient import TestClient

from lantern import cli
from lantern.config import Config
from lantern.core import Core
from lantern.forms import FormSettings
from lantern.ladder import RUNGS, Ladder, LadderError, TaskLadder, format_table
from lantern.memory import Memory
from lantern.policy import PRIVATE, WRITABLE, Policy, Rule
from lantern.router import FakeGlimmer, Router
from lantern.server import create_app
from fakes import call
from test_recovery import ScriptedForms, core_for
from test_scheduler import Routed, approve_all, finish


def ladder(tmp_path):
    return Ladder(Memory(tmp_path / "m.db"))


# ---- the rule --------------------------------------------------------------------------------
def test_optimistic_start_picks_the_cheapest_rung_that_can_do_the_kind(tmp_path):
    lad = ladder(tmp_path)
    assert lad.pick("plan").n == 3 and lad.pick("plan").effort == "low"
    assert lad.pick("file_op").n == 1 and lad.pick("extract").n == 2 and lad.pick("gui_action").n == 3
    assert RUNGS[5].badge == "Muse Wonder" and RUNGS[3].badge == "Muse Spark · low" and RUNGS[1].badge == "Rule"


def test_failures_move_a_kind_up_and_five_straight_successes_try_the_rung_below(tmp_path):
    lad = ladder(tmp_path)
    lad.record("plan", 3, True)
    lad.record("plan", 3, False)
    assert lad.pick("plan").n == 3                    # 5/6 with the optimistic start: still good enough
    lad.record("plan", 3, False)
    assert lad.pick("plan").n == 4                    # 5/7 < 0.8: Spark high from now on
    for _ in range(4):
        lad.record("plan", 4, True)
    assert lad.pick("plan").n == 4
    lad.record("plan", 4, True)                       # 5 straight successes on rung 4 ...
    assert lad.pick("plan").n == 3                    # ... so try rung 3 once more


def test_a_task_escalates_on_failure_for_the_rest_of_the_task(tmp_path):
    t = TaskLadder(ladder(tmp_path))
    assert t.pick("gui_action").n == 3
    assert t.failed("gui_action", 3).n == 5           # computer use: Spark, then Wonder
    assert t.pick("gui_action").n == 5
    assert t.failed("gui_action", 5) is None          # nothing above Wonder


def test_an_unaffordable_rung_gives_way_to_a_cheaper_one(tmp_path):
    lad = ladder(tmp_path)
    for _ in range(3):
        lad.record("plan", 4, True, cost_usd=0.40)
        lad.record("plan", 3, False, cost_usd=0.01)
    assert lad.pick("plan").n == 4
    assert lad.pick("plan", budget_left=0.10).n == 3


def test_private_content_never_goes_above_the_local_rung(tmp_path):
    lad = ladder(tmp_path)
    assert lad.pick("extract", private=True).n == 2
    assert lad.up("extract", 2, private=True) is None
    for _ in range(10):
        lad.record("extract", 2, False)
    assert lad.pick("extract", private=True).n == 2   # however badly it goes: never the cloud
    with pytest.raises(LadderError):
        lad.pick("plan", private=True)                # the agent's turns can't run on private content at all


# ---- in the scheduler ---------------------------------------------------------------------------
def test_a_failed_fill_moves_computer_use_up_to_wonder(tmp_path):
    forms = ScriptedForms([{"done": False, "verified": False, "submitted": False, "reason": "a custom dropdown"},
                           {"done": True, "verified": True, "submitted": True}])
    forms.settings = FormSettings(profile_dir=tmp_path / "p", media_dir=tmp_path / "m", vault_path=tmp_path / "v.json")
    models_used = []
    real_fill = forms.fill

    def fill(url, goal, **kw):
        models_used.append(forms.settings.model)
        return real_fill(url, goal, **kw)
    forms.fill = fill
    url = "http://127.0.0.1:9/apply"
    client = Routed({"permit": [([call("fill_form", "a", url=url, goal="permit")], ""),
                                ([call("fill_form", "b", url=url, goal="permit again")], ""), finish("f"), ([], "ok")]})
    core = core_for(tmp_path, client, forms=forms)
    approve_all(core)
    events = []
    core.subscribe(events.append)
    tid = core.submit("fill the permit form")
    assert core.wait_idle(5)
    assert models_used == ["muse-spark-1.3", "muse-wonder-1.0"]
    assert forms.settings.model == "muse-spark-1.3"           # put back after each fill
    fills = [e for e in events if e["type"] == "tool" and e["name"] == "fill_form"]
    assert [e["rung"] for e in fills] == [3, 5] and fills[1]["rung_badge"] == "Muse Wonder"
    table = {(r["kind"], r["rung"]): r for r in Ladder(core.memory).table()}
    assert table[("gui_action", 3)]["success"] == 0 and table[("gui_action", 5)]["success"] == 100
    assert core.memory.get_task(tid).status == "done"
    core.shutdown()


def test_every_model_call_and_tool_step_carries_its_rung(tmp_path):
    client = Routed({"folders": [([call("list_folders", "l")], ""), finish("f"), ([], "ok")]})
    core = core_for(tmp_path, client)
    events = []
    core.subscribe(events.append)
    core.submit("list my folders")
    assert core.wait_idle(5)
    usage = [e for e in events if e["type"] == "usage"]
    assert usage and all(e["rung"] == 3 and e["rung_badge"] == "Muse Spark · low" for e in usage)
    tool = next(e for e in events if e["type"] == "tool" and e["name"] == "list_folders")
    assert (tool["rung"], tool["rung_badge"], tool["kind"]) == (1, "Rule", "lookup")
    kinds = {(r["kind"], r["rung"]) for r in Ladder(core.memory).table()}
    assert ("plan", 3) in kinds and ("lookup", 1) in kinds
    core.shutdown()


def private_setup(tmp_path, backend):
    priv, docs = tmp_path / "Private", tmp_path / "docs"
    priv.mkdir()
    docs.mkdir(exist_ok=True)
    secret = "Annual salary: 42 lakh. Start date: December 1, 2026."
    (priv / "offer.txt").write_text(secret)
    policy = Policy([Rule.make(priv, PRIVATE), Rule.make(docs, WRITABLE)])
    cfg = Config(roots=[str(docs)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file, cfg.media_dir = str(tmp_path / "a.db"), str(tmp_path / "z.json"), str(tmp_path / "media")
    return cfg, policy, priv / "offer.txt", secret


def test_a_private_question_stays_on_rung_2_and_its_file_never_reaches_the_cloud(tmp_path):
    class Down(FakeGlimmer):
        def ask(self, system, user):
            from lantern.router import PrivateLaneError
            raise PrivateLaneError("the local model is not answering")
    cfg, policy, path, secret = private_setup(tmp_path, Down)
    client = Routed({"salary": [([call("ask_private", "p1", path=str(path), question="what is the salary?")], ""),
                                ([call("ask_private", "p2", path=str(path), question="the salary, please?")], ""),
                                finish("f", "couldn't read it locally"), ([], "ok")]})
    core = Core(cfg, client, policy=policy, router=Router(policy, Down()), reminder_tick=0, workers=2)
    core.scheduler.retry = {"base_delay": 0.01, "max_retries": 1}
    events = []
    core.subscribe(events.append)
    tid = core.submit("what's my salary in the offer letter")
    assert core.wait_idle(5)
    assert {(r["kind"], r["rung"]) for r in Ladder(core.memory).table() if r["kind"] == "extract"} == {("extract", 2)}
    steps = [e for e in events if e["type"] == "tool" and e["name"] == "ask_private"]
    assert all(e["rung"] == 2 for e in steps)
    for row in core.audit.for_task(tid):                       # nothing that left the laptop carries the file
        assert row["lane"] != "cloud" or "42 lakh" not in row["preview"]
    sent = json.dumps(client.fakes["salary"].requests)
    assert "42 lakh" not in sent and secret not in sent
    core.shutdown()


def test_a_private_answer_comes_from_rung_2(tmp_path):
    cfg, policy, path, _ = private_setup(tmp_path, FakeGlimmer)
    client = Routed({"start": [([call("ask_private", "p1", path=str(path), question="what is the start date?")], ""),
                               finish("f", "December 1"), ([], "ok")]})
    core = Core(cfg, client, policy=policy, router=Router(policy, FakeGlimmer()), reminder_tick=0, workers=2)
    events = []
    core.subscribe(events.append)
    core.submit("when do I start, per my offer letter")
    assert core.wait_idle(5)
    step = next(e for e in events if e["type"] == "tool" and e["name"] == "ask_private")
    assert step["rung"] == 2 and step["rung_badge"] == "Muse Glimmer (local)"
    core.shutdown()


# ---- the table ---------------------------------------------------------------------------------
def test_router_table_on_the_cli_and_the_api(tmp_path, monkeypatch, capsys):
    mem = Memory(tmp_path / "m.db")
    lad = Ladder(mem)
    lad.record("plan", 3, True, 0.002, 1.5)
    lad.record("plan", 3, False, 0.004, 2.5)
    lad.record("gui_action", 5, True, 0.05, 12.0)
    text = format_table(lad.table())
    assert "plan        3 Muse Spark · low          2      50%    $0.0030     2.0 s" in text
    assert "gui_action  5 Muse Wonder" in text
    cfg = Config(roots=[str(tmp_path)], db_path=str(tmp_path / "m.db"), audit_path=str(tmp_path / "a.db"),
                 zones_file=str(tmp_path / "z.json"), encrypt=False)
    monkeypatch.setattr(cli.config_mod, "load", lambda: cfg)
    cli.main(["--router"])
    assert "gui_action  5 Muse Wonder" in capsys.readouterr().out
    core = Core(cfg, Routed({}), reminder_tick=0, workers=1)
    api = TestClient(create_app(core, "tok"))
    body = api.get("/router", headers={"X-Lantern-Token": "tok"}).json()
    assert [r["n"] for r in body["rungs"]] == [0, 1, 2, 3, 4, 5]
    assert {(r["kind"], r["rung"]) for r in body["table"]} == {("plan", 3), ("gui_action", 5)}
    core.shutdown()
